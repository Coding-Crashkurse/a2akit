"""Redis-specific tests for RedisEventBus."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import suppress

import anyio
import pytest
from a2a.types import (
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("A2AKIT_TEST_REDIS_URL"),
    reason="Redis not configured (set A2AKIT_TEST_REDIS_URL)",
)

REDIS_URL = os.environ.get("A2AKIT_TEST_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
async def redis_event_bus():
    from a2akit.event_bus.redis import RedisEventBus

    prefix = f"a2akit:test:{uuid.uuid4().hex[:8]}:"
    eb = RedisEventBus(REDIS_URL, key_prefix=prefix)
    async with eb:
        yield eb
        await eb._redis.flushdb()


def _status_event(task_id: str, *, final: bool = False) -> TaskStatusUpdateEvent:
    state = TaskState.completed if final else TaskState.working
    return TaskStatusUpdateEvent(
        task_id=task_id,
        context_id="ctx-1",
        kind="status-update",
        status=TaskStatus(state=state),
        final=final,
    )


async def test_publish_returns_stream_id(redis_event_bus):
    """publish() returns a Redis Stream ID (e.g. '1234567890-0')."""
    event_id = await redis_event_bus.publish("task-1", _status_event("task-1"))
    assert event_id is not None
    assert "-" in event_id  # Redis stream IDs are like "123456-0"


async def test_live_subscriber_receives_events(redis_event_bus):
    """Pub/Sub subscriber gets events published after subscribing."""
    task_id = "task-live"
    received = []

    async def subscriber():
        async with redis_event_bus.subscribe(task_id) as stream:
            async for _eid, ev in stream:
                received.append(ev)
                if isinstance(ev, TaskStatusUpdateEvent) and ev.final:
                    break

    async with anyio.create_task_group() as tg:
        tg.start_soon(subscriber)
        await anyio.sleep(0.3)  # Let subscriber register
        await redis_event_bus.publish(task_id, _status_event(task_id))
        await redis_event_bus.publish(task_id, _status_event(task_id, final=True))

    assert len(received) == 2
    assert received[1].final is True


async def test_replay_after_event_id(redis_event_bus):
    """subscribe(after_event_id=...) replays from stream."""
    task_id = "task-replay"

    # Publish 3 events before subscribing
    id1 = await redis_event_bus.publish(task_id, _status_event(task_id))
    await redis_event_bus.publish(task_id, _status_event(task_id))
    await redis_event_bus.publish(task_id, _status_event(task_id, final=True))

    # Subscribe after_event_id=id1 — should get events 2 and 3
    received = []
    async with redis_event_bus.subscribe(task_id, after_event_id=id1) as stream:
        async for _eid, ev in stream:
            received.append(ev)
            if isinstance(ev, TaskStatusUpdateEvent) and ev.final:
                break

    assert len(received) == 2
    assert received[-1].final is True


async def test_cleanup_deletes_stream(redis_event_bus):
    """cleanup() sets a TTL on the replay stream instead of deleting immediately."""
    task_id = "task-cleanup"
    await redis_event_bus.publish(task_id, _status_event(task_id))

    stream_key = f"{redis_event_bus._stream_prefix}{task_id}"
    assert await redis_event_bus._redis.exists(stream_key)
    assert await redis_event_bus._redis.ttl(stream_key) == -1  # no TTL before cleanup

    await redis_event_bus.cleanup(task_id)
    ttl = await redis_event_bus._redis.ttl(stream_key)
    assert 0 < ttl <= 60  # EXPIRE 60 was set


async def test_final_event_ends_iterator(redis_event_bus):
    """TaskStatusUpdateEvent(final=True) terminates the subscribe iterator."""
    task_id = "task-final"
    received = []

    async def subscriber():
        async with redis_event_bus.subscribe(task_id) as stream:
            async for _eid, ev in stream:
                received.append(ev)

    async with anyio.create_task_group() as tg:
        tg.start_soon(subscriber)
        await anyio.sleep(0.3)
        await redis_event_bus.publish(task_id, _status_event(task_id))
        await redis_event_bus.publish(task_id, _status_event(task_id, final=True))

    assert len(received) == 2


async def test_key_prefix_isolation():
    """Two event buses with different prefixes don't interfere."""
    from a2akit.event_bus.redis import RedisEventBus

    prefix_a = f"a2akit:test:a:{uuid.uuid4().hex[:8]}:"
    prefix_b = f"a2akit:test:b:{uuid.uuid4().hex[:8]}:"

    async with (
        RedisEventBus(REDIS_URL, key_prefix=prefix_a) as bus_a,
        RedisEventBus(REDIS_URL, key_prefix=prefix_b) as bus_b,
    ):
        task_id = "task-iso"
        received_b = []

        async def sub_b():
            async with bus_b.subscribe(task_id) as stream:
                async for _eid, ev in stream:
                    received_b.append(ev)
                    break

        task = asyncio.create_task(sub_b())
        await asyncio.sleep(0.3)
        # Publish on bus_a — bus_b should not see it
        await bus_a.publish(task_id, _status_event(task_id, final=True))
        await asyncio.sleep(0.5)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert len(received_b) == 0

        await bus_a._redis.flushdb()
        await bus_b._redis.flushdb()


async def test_serialization_roundtrip_all_event_types(redis_event_bus):
    """Task, TaskStatusUpdateEvent, TaskArtifactUpdateEvent all roundtrip."""
    from a2akit.event_bus.redis import _deserialize_event, _serialize_event

    status = _status_event("t1")
    assert _deserialize_event(_serialize_event(status)).status.state == status.status.state

    from a2a.types import Message, Part, Role, TextPart

    from a2akit.schema import DirectReply

    msg = Message(
        role=Role.agent,
        parts=[Part(root=TextPart(text="hi"))],
        message_id="m1",
    )
    dr = DirectReply(message=msg)
    restored = _deserialize_event(_serialize_event(dr))
    assert isinstance(restored, DirectReply)
    assert restored.message.parts[0].root.text == "hi"
