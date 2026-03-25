"""Redis-specific tests for RedisCancelRegistry."""

from __future__ import annotations

import os
import uuid

import anyio
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("A2AKIT_TEST_REDIS_URL"),
    reason="Redis not configured (set A2AKIT_TEST_REDIS_URL)",
)

REDIS_URL = os.environ.get("A2AKIT_TEST_REDIS_URL", "redis://localhost:6379/0")


@pytest.fixture
async def redis_cancel_registry():
    from a2akit.broker.redis import RedisCancelRegistry

    prefix = f"a2akit:test:{uuid.uuid4().hex[:8]}:"
    cr = RedisCancelRegistry(REDIS_URL, key_prefix=prefix)
    yield cr
    await cr._redis.flushdb()
    await cr.close()


async def test_request_cancel_sets_key(redis_cancel_registry):
    """request_cancel() creates a Redis key."""
    task_id = "task-1"
    await redis_cancel_registry.request_cancel(task_id)

    cancel_key = f"{redis_cancel_registry._key_prefix}cancel:{task_id}"
    assert await redis_cancel_registry._redis.exists(cancel_key)


async def test_is_cancelled_true_after_request(redis_cancel_registry):
    """is_cancelled() returns True after request_cancel."""
    task_id = "task-2"
    assert await redis_cancel_registry.is_cancelled(task_id) is False
    await redis_cancel_registry.request_cancel(task_id)
    assert await redis_cancel_registry.is_cancelled(task_id) is True


async def test_cancel_scope_wait_unblocks_on_cancel(redis_cancel_registry):
    """scope.wait() unblocks when request_cancel() is called."""
    task_id = "task-3"
    scope = redis_cancel_registry.on_cancel(task_id)

    async with anyio.create_task_group() as tg:

        async def wait_for_cancel():
            with anyio.fail_after(5):
                await scope.wait()

        tg.start_soon(wait_for_cancel)
        await anyio.sleep(0.2)
        await redis_cancel_registry.request_cancel(task_id)

    assert scope.is_set() is True


async def test_cancel_scope_detects_pre_existing_cancel(redis_cancel_registry):
    """Scope created after request_cancel() starts as set."""
    task_id = "task-4"
    await redis_cancel_registry.request_cancel(task_id)

    scope = redis_cancel_registry.on_cancel(task_id)
    # Give the scope time to start
    await anyio.sleep(0.2)
    assert scope.is_set() is True


async def test_cleanup_deletes_key(redis_cancel_registry):
    """cleanup() deletes the cancel key."""
    task_id = "task-5"
    await redis_cancel_registry.request_cancel(task_id)
    assert await redis_cancel_registry.is_cancelled(task_id) is True

    await redis_cancel_registry.cleanup(task_id)
    assert await redis_cancel_registry.is_cancelled(task_id) is False


async def test_cleanup_idempotent(redis_cancel_registry):
    """Double cleanup() doesn't raise."""
    task_id = "task-6"
    await redis_cancel_registry.request_cancel(task_id)
    await redis_cancel_registry.cleanup(task_id)
    await redis_cancel_registry.cleanup(task_id)  # Should not raise


async def test_ttl_on_cancel_key(redis_cancel_registry):
    """Cancel key has expected TTL."""
    task_id = "task-7"
    await redis_cancel_registry.request_cancel(task_id)

    cancel_key = f"{redis_cancel_registry._key_prefix}cancel:{task_id}"
    ttl = await redis_cancel_registry._redis.ttl(cancel_key)
    # TTL should be set and > 0 (default 86400s)
    assert ttl > 0


async def test_key_prefix_isolation():
    """Two registries with different prefixes don't interfere."""
    from a2akit.broker.redis import RedisCancelRegistry

    prefix_a = f"a2akit:test:a:{uuid.uuid4().hex[:8]}:"
    prefix_b = f"a2akit:test:b:{uuid.uuid4().hex[:8]}:"

    cr_a = RedisCancelRegistry(REDIS_URL, key_prefix=prefix_a)
    cr_b = RedisCancelRegistry(REDIS_URL, key_prefix=prefix_b)

    await cr_a.request_cancel("task-x")
    assert await cr_a.is_cancelled("task-x") is True
    assert await cr_b.is_cancelled("task-x") is False

    await cr_a._redis.flushdb()
    await cr_a.close()
    await cr_b.close()
