"""Unit tests for InMemoryEventBus publish/subscribe and cleanup."""

from __future__ import annotations

import anyio
from a2a.types import TaskState, TaskStatus, TaskStatusUpdateEvent


async def test_publish_subscribe(event_bus):
    """Subscribing to a task_id receives events published to it."""
    task_id = "task-1"
    event = TaskStatusUpdateEvent(
        task_id=task_id,
        context_id="ctx-1",
        kind="status-update",
        status=TaskStatus(state=TaskState.working),
        final=False,
    )

    received = []

    async def subscriber():
        async with event_bus.subscribe(task_id) as stream:
            async for eid, ev in stream:
                received.append((eid, ev))
                break  # consume one and exit

    async with anyio.create_task_group() as tg:
        tg.start_soon(subscriber)
        # Small delay to let the subscriber register
        await anyio.sleep(0.05)
        await event_bus.publish(task_id, event)

    assert len(received) == 1
    eid, ev = received[0]
    assert isinstance(ev, TaskStatusUpdateEvent)
    assert ev.status.state == TaskState.working
    assert eid is not None


async def test_final_event_ends_iterator(event_bus):
    """Publishing a TaskStatusUpdateEvent with final=True ends the iterator."""
    task_id = "task-2"
    working_event = TaskStatusUpdateEvent(
        task_id=task_id,
        context_id="ctx-1",
        kind="status-update",
        status=TaskStatus(state=TaskState.working),
        final=False,
    )
    final_event = TaskStatusUpdateEvent(
        task_id=task_id,
        context_id="ctx-1",
        kind="status-update",
        status=TaskStatus(state=TaskState.completed),
        final=True,
    )

    received = []

    async def subscriber():
        async with event_bus.subscribe(task_id) as stream:
            async for _eid, ev in stream:
                received.append(ev)

    async with anyio.create_task_group() as tg:
        tg.start_soon(subscriber)
        await anyio.sleep(0.05)
        await event_bus.publish(task_id, working_event)
        await anyio.sleep(0.05)
        await event_bus.publish(task_id, final_event)

    assert len(received) == 2
    assert received[1].final is True


async def test_cleanup(event_bus):
    """After cleanup, no error is raised (idempotent)."""
    task_id = "task-3"

    # Publish an event so state exists
    event = TaskStatusUpdateEvent(
        task_id=task_id,
        context_id="ctx-1",
        kind="status-update",
        status=TaskStatus(state=TaskState.working),
        final=False,
    )
    await event_bus.publish(task_id, event)

    # Cleanup should succeed without errors
    await event_bus.cleanup(task_id)
    # Double cleanup must be idempotent
    await event_bus.cleanup(task_id)


async def test_publish_prunes_closed_subscriber_stream():
    """Publishing to an already-closed subscriber stream prunes it.

    Exercises the ``dead`` branch in ``InMemoryEventBus.publish`` — when
    ``send_nowait`` raises ``ClosedResourceError`` the stream is removed
    from the subscriber list so subsequent publishes do not retry it.

    We install the subscriber stream into the internal list manually and
    close it; normal ``subscribe()`` exit would prune the stream from the
    list before ``publish`` ever sees it, so we cannot trigger this path
    through the public API.
    """
    from a2akit.event_bus.memory import InMemoryEventBus

    eb = InMemoryEventBus()
    async with eb:
        task_id = "task-closed-sub"
        send_stream, recv_stream = anyio.create_memory_object_stream[
            tuple[str, TaskStatusUpdateEvent]
        ](max_buffer_size=16)
        async with eb._subscriber_lock:
            eb._subs.setdefault(task_id, []).append(send_stream)
        await send_stream.aclose()

        event = TaskStatusUpdateEvent(
            task_id=task_id,
            context_id="ctx-1",
            kind="status-update",
            status=TaskStatus(state=TaskState.working),
            final=False,
        )
        await eb.publish(task_id, event)
        # Subscriber list cleaned up because the stream was closed.
        assert not eb._subs.get(task_id)

        await recv_stream.aclose()


async def test_publish_drops_event_when_buffer_full():
    """A slow consumer triggers ``WouldBlock``; event dropped for that subscriber.

    The replay buffer still holds the event so reconnection semantics are
    preserved. Exercises the ``except anyio.WouldBlock`` branch.
    """
    from a2akit.event_bus.memory import InMemoryEventBus

    eb = InMemoryEventBus(event_buffer=1)
    async with eb:
        task_id = "task-slow"
        async with eb.subscribe(task_id) as _stream:
            # Flood without ever consuming — after the buffer fills,
            # subsequent publishes hit WouldBlock for this subscriber.
            for _ in range(3):
                await eb.publish(
                    task_id,
                    TaskStatusUpdateEvent(
                        task_id=task_id,
                        context_id="ctx-1",
                        kind="status-update",
                        status=TaskStatus(state=TaskState.working),
                        final=False,
                    ),
                )
            # The subscriber stays in the list (not broken, just slow).
            assert eb._subs.get(task_id)


async def test_subscribe_with_invalid_after_event_id(event_bus):
    """A non-integer ``after_event_id`` falls back to replay-from-start.

    Exercises the ``except (ValueError, TypeError)`` branch in the replay
    phase of ``_iter_events``.
    """
    task_id = "task-invalid-after"
    event = TaskStatusUpdateEvent(
        task_id=task_id,
        context_id="ctx-1",
        kind="status-update",
        status=TaskStatus(state=TaskState.completed),
        final=True,
    )
    # Publish first so the event sits in the replay buffer.
    await event_bus.publish(task_id, event)

    received = []
    async with event_bus.subscribe(task_id, after_event_id="not-a-number") as stream:
        async for _eid, ev in stream:
            received.append(ev)
            if ev.final:
                break

    assert len(received) == 1
    assert received[0].final is True
