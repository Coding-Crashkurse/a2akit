"""Unit tests for InMemoryCancelRegistry."""

from __future__ import annotations

from a2akit.broker.memory import InMemoryCancelRegistry


async def test_request_cancel():
    """Requesting cancellation marks the task as cancelled."""
    registry = InMemoryCancelRegistry()
    task_id = "task-1"

    assert await registry.is_cancelled(task_id) is False
    await registry.request_cancel(task_id)
    assert await registry.is_cancelled(task_id) is True


async def test_on_cancel_scope():
    """on_cancel returns a scope that is set when cancel is requested."""
    registry = InMemoryCancelRegistry()
    task_id = "task-2"

    scope = registry.on_cancel(task_id)
    assert scope.is_set() is False

    await registry.request_cancel(task_id)
    assert scope.is_set() is True


async def test_cleanup():
    """After cleanup, the task is no longer marked as cancelled."""
    registry = InMemoryCancelRegistry()
    task_id = "task-3"

    await registry.request_cancel(task_id)
    assert await registry.is_cancelled(task_id) is True

    await registry.cleanup(task_id)
    assert await registry.is_cancelled(task_id) is False
