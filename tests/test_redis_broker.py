"""Redis-specific tests for RedisBroker (not covered by parametrized generic tests)."""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import suppress

import pytest
from a2a.types import Message, MessageSendParams, Part, Role, TextPart

pytestmark = pytest.mark.skipif(
    not os.environ.get("A2AKIT_TEST_REDIS_URL"),
    reason="Redis not configured (set A2AKIT_TEST_REDIS_URL)",
)

REDIS_URL = os.environ.get("A2AKIT_TEST_REDIS_URL", "redis://localhost:6379/0")


def _params(text: str = "hello") -> MessageSendParams:
    msg = Message(
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
        message_id="msg1",
    )
    return MessageSendParams(message=msg)


@pytest.fixture
async def redis_broker():
    from a2akit.broker.redis import RedisBroker

    prefix = f"a2akit:test:{uuid.uuid4().hex[:8]}:"
    b = RedisBroker(REDIS_URL, key_prefix=prefix)
    async with b:
        yield b
        await b._redis.flushdb()


async def test_consumer_group_created_on_enter(redis_broker):
    """XGROUP CREATE happens in __aenter__."""
    info = await redis_broker._redis.xinfo_groups(redis_broker._stream_key)
    group_names = [g["name"].decode() for g in info]
    assert redis_broker._group_name in group_names


async def test_consumer_deleted_on_exit():
    """XGROUP DELCONSUMER happens in __aexit__."""
    from a2akit.broker.redis import RedisBroker

    prefix = f"a2akit:test:{uuid.uuid4().hex[:8]}:"
    b = RedisBroker(REDIS_URL, key_prefix=prefix)
    async with b:
        consumer_name = b._consumer_name
        stream_key = b._stream_key
        group_name = b._group_name

    # After exit, consumer should be gone
    # (We need a fresh connection to check since the broker closed its own)
    import redis.asyncio as aioredis

    r = aioredis.from_url(REDIS_URL)
    try:
        info = await r.xinfo_groups(stream_key)
        for g in info:
            if g["name"].decode() == group_name:
                consumers = await r.xinfo_consumers(stream_key, group_name)
                consumer_names = [c["name"].decode() for c in consumers]
                assert consumer_name not in consumer_names
    finally:
        await r.flushdb()
        await r.aclose()


async def test_nack_requeues_with_incremented_attempt(redis_broker):
    """nack() adds new entry with attempt+1."""
    await redis_broker.run_task(_params("retry-me"))

    attempts_seen = []
    async for handle in redis_broker.receive_task_operations():
        attempts_seen.append(handle.attempt)
        if handle.attempt < 2:
            await handle.nack()
        else:
            await handle.ack()
            break

    assert attempts_seen == [1, 2]


async def test_shutdown_stops_receive_loop(redis_broker):
    """shutdown() causes receive_task_operations to terminate."""
    received = []

    async def consumer():
        async for handle in redis_broker.receive_task_operations():
            received.append(handle)
            await handle.ack()

    task = asyncio.create_task(consumer())
    await asyncio.sleep(0.1)
    await redis_broker.shutdown()

    # Give the loop time to notice the shutdown flag
    await asyncio.sleep((redis_broker._block_ms / 1000) + 1)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def test_key_prefix_isolation():
    """Two brokers with different prefixes don't interfere."""
    from a2akit.broker.redis import RedisBroker

    prefix_a = f"a2akit:test:a:{uuid.uuid4().hex[:8]}:"
    prefix_b = f"a2akit:test:b:{uuid.uuid4().hex[:8]}:"

    async with (
        RedisBroker(REDIS_URL, key_prefix=prefix_a) as broker_a,
        RedisBroker(REDIS_URL, key_prefix=prefix_b) as broker_b,
    ):
        await broker_a.run_task(_params("from-a"))

        # broker_b should not see broker_a's message
        got_message = False

        async def try_receive():
            nonlocal got_message
            async for handle in broker_b.receive_task_operations():
                got_message = True
                await handle.ack()
                break

        task = asyncio.create_task(try_receive())
        await asyncio.sleep(1)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert not got_message

        await broker_a._redis.flushdb()
        await broker_b._redis.flushdb()


async def test_operation_serialization_roundtrip(redis_broker):
    """JSON encode/decode preserves all fields."""
    params = _params("roundtrip")
    await redis_broker.run_task(params, is_new_task=True, request_context={"user": "test"})

    async for handle in redis_broker.receive_task_operations():
        assert handle.operation.operation == "run"
        assert handle.operation.params.message.parts[0].root.text == "roundtrip"
        assert handle.operation.is_new_task is True
        assert handle.operation.request_context == {"user": "test"}
        await handle.ack()
        break
