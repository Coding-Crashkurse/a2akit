"""Unit tests for CancelRegistry — runs against both InMemory and Redis backends."""

from __future__ import annotations


async def test_request_cancel(cancel_registry):
    """Requesting cancellation marks the task as cancelled."""
    task_id = "task-1"

    assert await cancel_registry.is_cancelled(task_id) is False
    await cancel_registry.request_cancel(task_id)
    assert await cancel_registry.is_cancelled(task_id) is True


async def test_on_cancel_scope(cancel_registry):
    """on_cancel returns a scope that is set when cancel is requested."""
    task_id = "task-2"

    scope = cancel_registry.on_cancel(task_id)
    assert scope.is_set() is False

    await cancel_registry.request_cancel(task_id)
    # Give the scope a moment to pick up the signal (Redis uses Pub/Sub)
    import anyio

    with anyio.fail_after(2):
        await scope.wait()
    assert scope.is_set() is True


async def test_cleanup(cancel_registry):
    """After cleanup, the task is no longer marked as cancelled."""
    task_id = "task-3"

    await cancel_registry.request_cancel(task_id)
    assert await cancel_registry.is_cancelled(task_id) is True

    await cancel_registry.cleanup(task_id)
    assert await cancel_registry.is_cancelled(task_id) is False
