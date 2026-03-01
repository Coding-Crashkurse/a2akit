"""Tests for cancel_task_in_storage — covers cancel.py lines 55, 59-95."""

from __future__ import annotations

import uuid

from a2a.types import (
    Message,
    Part,
    Role,
    Task,
    TaskState,
    TextPart,
)

from a2akit.cancel import cancel_task_in_storage
from a2akit.event_bus.memory import InMemoryEventBus
from a2akit.event_emitter import DefaultEventEmitter
from a2akit.storage.base import ConcurrencyError
from a2akit.storage.memory import InMemoryStorage


async def _create_task(storage: InMemoryStorage, state: TaskState = TaskState.working) -> Task:
    """Helper to create a task in a given state."""
    msg = Message(
        role=Role.user,
        parts=[Part(TextPart(text="hello"))],
        message_id=str(uuid.uuid4()),
    )
    task = await storage.create_task("ctx-1", msg)
    if state != TaskState.submitted:
        await storage.update_task(task.id, state=state)
    return task


async def test_cancel_task_not_found():
    """cancel_task_in_storage with non-existent task_id returns immediately."""
    storage = InMemoryStorage()
    async with InMemoryEventBus() as event_bus:
        emitter = DefaultEventEmitter(event_bus, storage)
        # Should not raise
        await cancel_task_in_storage(storage, emitter, "nonexistent-id", None)


async def test_cancel_task_already_terminal():
    """cancel_task_in_storage with a task already in terminal state returns immediately."""
    storage = InMemoryStorage()
    async with InMemoryEventBus() as event_bus:
        emitter = DefaultEventEmitter(event_bus, storage)
        task = await _create_task(storage, TaskState.working)
        # Force task to completed
        await storage.update_task(task.id, state=TaskState.completed)

        # Should return immediately without error
        await cancel_task_in_storage(storage, emitter, task.id, "ctx-1")

        # Task should still be completed, not canceled
        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.completed


async def test_cancel_task_success():
    """cancel_task_in_storage successfully cancels a working task."""
    storage = InMemoryStorage()
    async with InMemoryEventBus() as event_bus:
        emitter = DefaultEventEmitter(event_bus, storage)
        task = await _create_task(storage, TaskState.working)

        await cancel_task_in_storage(storage, emitter, task.id, "ctx-1", reason="User canceled")

        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.canceled


async def test_cancel_task_with_concurrency_retry():
    """cancel_task_in_storage retries on ConcurrencyError when task is still non-terminal."""
    storage = InMemoryStorage()
    async with InMemoryEventBus() as event_bus:
        emitter = DefaultEventEmitter(event_bus, storage)
        task = await _create_task(storage, TaskState.working)

        # Corrupt the version to trigger ConcurrencyError on first attempt
        original_update = storage.update_task
        call_count = 0

        async def patched_update(task_id, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1 and kwargs.get("state") == TaskState.canceled:
                raise ConcurrencyError("simulated version mismatch")
            return await original_update(task_id, **kwargs)

        storage.update_task = patched_update

        await cancel_task_in_storage(storage, emitter, task.id, "ctx-1")

        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.canceled


async def test_cancel_task_concurrency_retry_task_became_terminal():
    """cancel_task_in_storage ConcurrencyError retry when task is now terminal returns immediately."""
    storage = InMemoryStorage()
    async with InMemoryEventBus() as event_bus:
        emitter = DefaultEventEmitter(event_bus, storage)
        task = await _create_task(storage, TaskState.working)

        original_update = storage.update_task
        call_count = 0

        async def patched_update(task_id, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1 and kwargs.get("state") == TaskState.canceled:
                # Simulate another writer completing the task between load and write
                await original_update(task_id, state=TaskState.completed)
                raise ConcurrencyError("simulated version mismatch")
            return await original_update(task_id, **kwargs)

        storage.update_task = patched_update

        # Should not raise -- ConcurrencyError followed by terminal state => return
        await cancel_task_in_storage(storage, emitter, task.id, "ctx-1")

        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.completed


async def test_cancel_task_concurrency_retry_task_vanished():
    """cancel_task_in_storage ConcurrencyError retry when task no longer exists."""
    storage = InMemoryStorage()
    async with InMemoryEventBus() as event_bus:
        emitter = DefaultEventEmitter(event_bus, storage)
        task = await _create_task(storage, TaskState.working)

        original_update = storage.update_task
        call_count = 0

        async def patched_update(task_id, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1 and kwargs.get("state") == TaskState.canceled:
                # Delete the task to simulate it vanishing
                await storage.delete_task(task_id)
                raise ConcurrencyError("simulated version mismatch")
            return await original_update(task_id, **kwargs)

        storage.update_task = patched_update

        # Should return without error when task is gone
        await cancel_task_in_storage(storage, emitter, task.id, "ctx-1")
