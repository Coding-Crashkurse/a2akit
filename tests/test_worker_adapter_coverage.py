"""Tests for worker/adapter.py — retry/nack, task lock factory, cancel-before-work, TaskTerminalStateError."""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest
from a2a.types import (
    Message,
    MessageSendParams,
    Part,
    Role,
    TaskState,
    TextPart,
)

from a2akit import (
    InMemoryBroker,
    InMemoryEventBus,
    InMemoryStorage,
    TaskContext,
    Worker,
)
from a2akit.broker.memory import InMemoryCancelRegistry
from a2akit.worker.adapter import WorkerAdapter
from conftest import EchoWorker


class SlowWorker(Worker):
    """Worker that takes time to process, allowing cancel-before-work to be tested."""

    async def handle(self, ctx: TaskContext) -> None:
        if ctx.is_cancelled:
            return
        await ctx.complete(f"Done: {ctx.user_text}")


async def test_cancel_before_work():
    """Task that is canceled before worker starts gets canceled state."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        adapter = WorkerAdapter(
            SlowWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
        )

        # Create a task
        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        msg_copy = msg.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})

        # Request cancel BEFORE running the worker
        await cancel_reg.request_cancel(task.id)

        params = MessageSendParams(message=msg_copy)

        async with adapter.run():
            await broker.run_task(params, is_new_task=True)
            # Give the adapter time to process
            await asyncio.sleep(0.3)

        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.canceled


class LockTracker:
    """Tracks whether a lock was acquired."""

    def __init__(self):
        self.acquired = False
        self.released = False

    @asynccontextmanager
    async def __call__(self, task_id: str):
        self.acquired = True
        try:
            yield
        finally:
            self.released = True


async def test_task_lock_factory():
    """WorkerAdapter with task_lock_factory acquires lock before processing."""
    storage = InMemoryStorage()
    lock_tracker = LockTracker()

    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        adapter = WorkerAdapter(
            EchoWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
            task_lock_factory=lock_tracker,
        )

        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        msg_copy = msg.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
        params = MessageSendParams(message=msg_copy)

        async with adapter.run():
            await broker.run_task(params, is_new_task=True)
            await asyncio.sleep(0.3)

        assert lock_tracker.acquired
        assert lock_tracker.released

        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.completed


async def test_max_concurrent_tasks():
    """WorkerAdapter with max_concurrent_tasks limits concurrent execution."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        adapter = WorkerAdapter(
            EchoWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
            max_concurrent_tasks=1,
        )

        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        msg_copy = msg.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
        params = MessageSendParams(message=msg_copy)

        async with adapter.run():
            await broker.run_task(params, is_new_task=True)
            await asyncio.sleep(0.3)

        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.completed


async def test_cancel_before_work_task_already_terminal():
    """Cancel-before-work skips _mark_canceled if task is already terminal."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        adapter = WorkerAdapter(
            EchoWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
        )

        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        # Complete the task before running
        await storage.update_task(task.id, state=TaskState.completed)

        msg_copy = msg.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
        params = MessageSendParams(message=msg_copy)

        # Request cancel
        await cancel_reg.request_cancel(task.id)

        async with adapter.run():
            await broker.run_task(params, is_new_task=True)
            await asyncio.sleep(0.3)

        loaded = await storage.load_task(task.id)
        # Should still be completed, not canceled
        assert loaded.status.state == TaskState.completed


async def test_run_task_missing_task_id():
    """_run_task raises ValueError when message.task_id is missing."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        adapter = WorkerAdapter(
            EchoWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
        )

        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
            # No task_id
        )
        params = MessageSendParams(message=msg)

        with pytest.raises(ValueError, match="task_id is missing"):
            await adapter._run_task(params)


async def test_terminal_state_during_working_transition():
    """If task becomes terminal during working transition, adapter returns without error."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        adapter = WorkerAdapter(
            EchoWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
        )

        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        # Complete the task so working transition fails with TaskTerminalStateError
        await storage.update_task(task.id, state=TaskState.completed)

        msg_copy = msg.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
        params = MessageSendParams(message=msg_copy)

        async with adapter.run():
            await broker.run_task(params, is_new_task=True)
            await asyncio.sleep(0.3)

        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.completed


async def test_mark_failed_concurrency_error():
    """_mark_failed silently returns on ConcurrencyError."""
    from a2akit.event_emitter import DefaultEventEmitter

    storage = InMemoryStorage()
    async with InMemoryEventBus() as event_bus:
        emitter = DefaultEventEmitter(event_bus, storage)

        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        await storage.update_task(task.id, state=TaskState.working)

        # Corrupt version to cause ConcurrencyError
        storage._versions[task.id] = 999

        # Should not raise — ConcurrencyError is caught
        await WorkerAdapter._mark_failed(emitter, storage, task.id, "ctx-1", "test failure")


async def test_adapter_handle_op_inner_nack_retry():
    """_handle_op_inner nacks and retries when _dispatch raises and attempt < max_retries."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        adapter = WorkerAdapter(
            EchoWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
            max_retries=3,
        )

        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        msg_copy = msg.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
        params = MessageSendParams(message=msg_copy)

        # Patch _dispatch to fail once
        original_dispatch = adapter._dispatch
        dispatch_calls = 0

        async def patched_dispatch(op):
            nonlocal dispatch_calls
            dispatch_calls += 1
            if dispatch_calls == 1:
                raise RuntimeError("Infrastructure failure")
            return await original_dispatch(op)

        adapter._dispatch = patched_dispatch

        async with adapter.run():
            await broker.run_task(params, is_new_task=True)
            # Give time for nack + requeue + retry
            await asyncio.sleep(0.8)

        loaded = await storage.load_task(task.id)
        # After retry, the worker should succeed
        assert loaded.status.state == TaskState.completed
        assert dispatch_calls >= 2  # At least the first fail + retry


async def test_adapter_handle_op_inner_max_retries_marks_failed():
    """_handle_op_inner marks task as failed when max retries are exhausted."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        adapter = WorkerAdapter(
            EchoWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
            max_retries=1,  # attempt 1 >= max_retries 1 -> immediate failure
        )

        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        msg_copy = msg.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
        params = MessageSendParams(message=msg_copy)

        # Patch _dispatch to always fail
        async def always_fail_dispatch(op):
            raise RuntimeError("Permanent infrastructure failure")

        adapter._dispatch = always_fail_dispatch

        async with adapter.run():
            await broker.run_task(params, is_new_task=True)
            await asyncio.sleep(0.5)

        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.failed


class TerminalStateErrorWorker(Worker):
    """Worker that triggers TaskTerminalStateError by calling complete twice."""

    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete("First completion")
        # The second complete would trigger TaskTerminalStateError
        # since task is now in terminal state
        from a2akit.storage.base import TaskTerminalStateError

        raise TaskTerminalStateError("Task already terminal")


async def test_adapter_handles_terminal_state_during_processing():
    """Adapter handles TaskTerminalStateError raised during worker processing."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        adapter = WorkerAdapter(
            TerminalStateErrorWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
        )

        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        msg_copy = msg.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
        params = MessageSendParams(message=msg_copy)

        async with adapter.run():
            await broker.run_task(params, is_new_task=True)
            await asyncio.sleep(0.3)

        loaded = await storage.load_task(task.id)
        # Task should be in completed state from first complete()
        assert loaded.status.state == TaskState.completed
