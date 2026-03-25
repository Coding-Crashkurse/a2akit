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
            async for ev in stream:
                received.append(ev)
                break  # consume one and exit

    async with anyio.create_task_group() as tg:
        tg.start_soon(subscriber)
        # Small delay to let the subscriber register
        await anyio.sleep(0.05)
        await event_bus.publish(task_id, event)

    assert len(received) == 1
    assert isinstance(received[0], TaskStatusUpdateEvent)
    assert received[0].status.state == TaskState.working


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
            async for ev in stream:
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
