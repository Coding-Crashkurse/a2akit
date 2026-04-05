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


async def test_cleanup_release_key_false_preserves_cancel_signal(cancel_registry):
    """``release_key=False`` must not drop the cancel signal.

    Regression: WorkerAdapter calls cleanup() at the end of every turn,
    including non-terminal ``input_required`` pauses. If cleanup dropped
    the cancel key unconditionally, a ``request_cancel`` that arrived
    between turns would be silently lost until the force-cancel timeout
    fired (up to 60s).
    """
    task_id = "task-release-key"

    await cancel_registry.request_cancel(task_id)
    assert await cancel_registry.is_cancelled(task_id) is True

    # Non-terminal turn end — key must survive.
    await cancel_registry.cleanup(task_id, release_key=False)
    assert await cancel_registry.is_cancelled(task_id) is True

    # Terminal cleanup clears the signal.
    await cancel_registry.cleanup(task_id, release_key=True)
    assert await cancel_registry.is_cancelled(task_id) is False


async def test_cleanup_release_key_false_releases_scope_resources(cancel_registry):
    """Scope resources are released even when the cancel key is preserved.

    On Redis this means the Pub/Sub subscription is torn down, so we
    don't leak one connection per turn on a long-lived ``input_required``
    task that receives many follow-ups.
    """
    task_id = "task-scope-release"
    scope = cancel_registry.on_cancel(task_id)
    assert scope.is_set() is False

    # Should not raise even though we hold a live scope reference.
    await cancel_registry.cleanup(task_id, release_key=False)

    # Key must still be absent (never set).
    assert await cancel_registry.is_cancelled(task_id) is False
