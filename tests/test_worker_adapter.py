"""Tests for WorkerAdapter error-handling behaviour, exercised through HTTP."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import anyio
import httpx
from a2a.types import (
    Message,
    Part,
    Role,
    TextPart,
)
from a2a_pydantic.v10 import SendMessageRequest, TaskState
from asgi_lifespan import LifespanManager

from a2akit import InMemoryEventBus, InMemoryStorage, TaskContext, Worker
from a2akit.broker import OperationHandle
from a2akit.broker.base import _RunTask
from a2akit.broker.memory import InMemoryBroker, InMemoryCancelRegistry
from a2akit.event_emitter import DefaultEventEmitter
from a2akit.worker.adapter import WorkerAdapter
from conftest import CrashWorker, NoLifecycleWorker, _make_app


async def test_adapter_marks_failed_on_crash():
    """Worker crash -> task marked failed by adapter."""
    app = _make_app(CrashWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "boom"}],
                },
                "configuration": {"blocking": True},
            }
            r = await client.post("/v1/message:send", json=body)
            assert r.status_code == 200
            data = r.json()
            assert data["status"]["state"] == "failed"


async def test_adapter_marks_failed_on_no_lifecycle():
    """Worker returns without lifecycle call -> adapter marks failed."""
    app = _make_app(NoLifecycleWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "hello"}],
                },
                "configuration": {"blocking": True},
            }
            r = await client.post("/v1/message:send", json=body)
            assert r.status_code == 200
            data = r.json()
            assert data["status"]["state"] == "failed"


# ---------------------------------------------------------------------------
# Unit tests for adapter internals (poison pill, nack fallthrough, cleanup)
# ---------------------------------------------------------------------------


def _make_handle(op, *, attempt=1, ack_side_effect=None, nack_side_effect=None):
    """Create a mock OperationHandle."""
    handle = MagicMock(spec=OperationHandle)
    handle.operation = op
    handle.attempt = attempt
    handle.ack = AsyncMock(side_effect=ack_side_effect)
    handle.nack = AsyncMock(side_effect=nack_side_effect)
    return handle


def _make_run_op(task_id="task-1", context_id="ctx-1"):
    """Create a _RunTask operation."""
    from a2a_pydantic.v10 import Message as V10Message
    from a2a_pydantic.v10 import Part as V10Part
    from a2a_pydantic.v10 import Role as V10Role

    msg = V10Message(
        role=V10Role.role_user,
        parts=[V10Part(text="hello")],
        message_id=str(uuid.uuid4()),
        task_id=task_id,
        context_id=context_id,
    )
    params = SendMessageRequest(message=msg)
    return _RunTask(operation="run", params=params, is_new_task=True)


async def test_poison_pill_precheck_marks_failed():
    """When handle.attempt > max_retries, task should be marked failed without dispatch."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        emitter = DefaultEventEmitter(event_bus, storage)

        class DummyWorker(Worker):
            async def handle(self, ctx: TaskContext) -> None:
                await ctx.complete("ok")

        adapter = WorkerAdapter(
            DummyWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
            max_retries=3,
            emitter=emitter,
        )

        # Create the task in storage first, then build an op with the real task_id
        init_msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", init_msg)
        task_id = task.id

        op = _make_run_op(task_id=task_id)

        # Simulate attempt > max_retries (poison pill)
        handle = _make_handle(op, attempt=4)
        await adapter._handle_op_inner(handle)

        # Task should be marked failed
        loaded = await storage.load_task(task_id)
        assert loaded is not None
        assert loaded.status.state == TaskState.task_state_failed

        # Handle should have been ack'd
        handle.ack.assert_awaited_once()


async def test_poison_pill_ack_failure_does_not_raise():
    """Poison pill path: if ack() fails, it should not propagate."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        emitter = DefaultEventEmitter(event_bus, storage)

        adapter = WorkerAdapter(
            MagicMock(spec=Worker),
            broker,
            storage,
            event_bus,
            cancel_reg,
            max_retries=2,
            emitter=emitter,
        )

        init_msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", init_msg)
        op = _make_run_op(task_id=task.id)

        handle = _make_handle(op, attempt=5, ack_side_effect=RuntimeError("ack broken"))
        # Should not raise
        await adapter._handle_op_inner(handle)


async def test_nack_failure_falls_through_to_mark_failed():
    """When nack() raises, the error path should fall through and mark the task failed."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        emitter = DefaultEventEmitter(event_bus, storage)

        class CrashingWorker(Worker):
            async def handle(self, ctx: TaskContext) -> None:
                raise RuntimeError("worker boom")

        adapter = WorkerAdapter(
            CrashingWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
            max_retries=3,
            emitter=emitter,
        )

        init_msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", init_msg)
        task_id = task.id
        op = _make_run_op(task_id=task_id)

        # attempt=1, nack will raise -> falls through to mark_failed path
        handle = _make_handle(op, attempt=1, nack_side_effect=RuntimeError("nack broken"))
        await adapter._handle_op_inner(handle)

        loaded = await storage.load_task(task_id)
        assert loaded is not None
        assert loaded.status.state == TaskState.task_state_failed


async def test_cooperative_cancel_marks_canceled():
    """Worker that returns without lifecycle call when cancel is set -> marked canceled."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        emitter = DefaultEventEmitter(event_bus, storage)

        class CoopCancelWorker(Worker):
            async def handle(self, ctx: TaskContext) -> None:
                # Simulate checking cancel and returning without lifecycle call
                if ctx.is_cancelled:
                    return

        adapter = WorkerAdapter(
            CoopCancelWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
            emitter=emitter,
        )

        init_msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task_obj = await storage.create_task("ctx-1", init_msg)
        task_id = task_obj.id
        op = _make_run_op(task_id=task_id)

        # Request cancel before the worker runs
        await cancel_reg.request_cancel(task_id)

        handle = _make_handle(op, attempt=1)
        await adapter._handle_op_inner(handle)

        loaded = await storage.load_task(task_id)
        assert loaded is not None
        assert loaded.status.state == TaskState.task_state_canceled


async def test_cleanup_only_for_terminal_tasks():
    """event_bus.cleanup is called only for terminal state tasks."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        emitter = DefaultEventEmitter(event_bus, storage)

        class InputWorker(Worker):
            async def handle(self, ctx: TaskContext) -> None:
                await ctx.request_input("Need more info")

        adapter = WorkerAdapter(
            InputWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
            emitter=emitter,
        )

        init_msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task_obj = await storage.create_task("ctx-1", init_msg)
        task_id = task_obj.id
        op = _make_run_op(task_id=task_id)

        # Spy on event_bus.cleanup
        original_cleanup = event_bus.cleanup
        cleanup_calls = []

        async def spy_cleanup(tid):
            cleanup_calls.append(tid)
            return await original_cleanup(tid)

        event_bus.cleanup = spy_cleanup

        handle = _make_handle(op, attempt=1)
        await adapter._handle_op_inner(handle)

        loaded = await storage.load_task(task_id)
        assert loaded is not None
        assert loaded.status.state == TaskState.task_state_input_required

        # event_bus.cleanup should NOT have been called for non-terminal
        assert task_id not in cleanup_calls


async def test_cleanup_runs_for_completed_task():
    """event_bus.cleanup IS called for a completed (terminal) task."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        emitter = DefaultEventEmitter(event_bus, storage)

        class CompleteWorker(Worker):
            async def handle(self, ctx: TaskContext) -> None:
                await ctx.complete("done")

        adapter = WorkerAdapter(
            CompleteWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
            emitter=emitter,
        )

        init_msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task_obj = await storage.create_task("ctx-1", init_msg)
        task_id = task_obj.id
        op = _make_run_op(task_id=task_id)

        cleanup_calls = []
        original_cleanup = event_bus.cleanup

        async def spy_cleanup(tid):
            cleanup_calls.append(tid)
            return await original_cleanup(tid)

        event_bus.cleanup = spy_cleanup

        handle = _make_handle(op, attempt=1)
        await adapter._handle_op_inner(handle)

        loaded = await storage.load_task(task_id)
        assert loaded is not None
        assert loaded.status.state == TaskState.task_state_completed

        # event_bus.cleanup SHOULD have been called for terminal task
        assert task_id in cleanup_calls


async def test_cleanup_exception_does_not_propagate():
    """Exceptions in event_bus.cleanup and cancel_registry.cleanup are caught."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        emitter = DefaultEventEmitter(event_bus, storage)

        class CompleteWorker(Worker):
            async def handle(self, ctx: TaskContext) -> None:
                await ctx.complete("done")

        adapter = WorkerAdapter(
            CompleteWorker(),
            broker,
            storage,
            event_bus,
            cancel_reg,
            emitter=emitter,
        )

        init_msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task_obj = await storage.create_task("ctx-1", init_msg)
        task_id = task_obj.id
        op = _make_run_op(task_id=task_id)

        # Make cleanup raise
        async def broken_cleanup(tid):
            raise RuntimeError("cleanup broken")

        event_bus.cleanup = broken_cleanup
        cancel_reg.cleanup = broken_cleanup  # type: ignore[assignment]

        handle = _make_handle(op, attempt=1)
        # Should not raise even though both cleanups fail
        await adapter._handle_op_inner(handle)


async def test_broker_loop_survives_iterator_crash():
    """An exception escaping the broker's receive iterator must not kill the
    consumption loop — the adapter retries and keeps consuming."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as inner, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        emitter = DefaultEventEmitter(event_bus, storage)

        class FlakyBroker:
            """Delegates to an InMemoryBroker but crashes the first receive."""

            def __init__(self, wrapped):
                self._wrapped = wrapped
                self.crashes = 0

            async def receive_task_operations(self):
                if self.crashes == 0:
                    self.crashes += 1
                    raise ValueError("malformed stream entry")
                async for handle in self._wrapped.receive_task_operations():
                    yield handle

            def __getattr__(self, name):
                return getattr(self._wrapped, name)

        flaky = FlakyBroker(inner)

        class CompleteWorker(Worker):
            async def handle(self, ctx: TaskContext) -> None:
                await ctx.complete("done")

        adapter = WorkerAdapter(
            CompleteWorker(),
            flaky,  # type: ignore[arg-type]
            storage,
            event_bus,
            cancel_reg,
            emitter=emitter,
        )

        init_msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task_obj = await storage.create_task("ctx-1", init_msg)
        op = _make_run_op(task_id=task_obj.id)

        with anyio.fail_after(15):
            async with adapter.run():
                await inner.run_task(op.params, is_new_task=True)
                while True:
                    loaded = await storage.load_task(task_obj.id)
                    if loaded and loaded.status.state == TaskState.task_state_completed:
                        break
                    await anyio.sleep(0.05)

        assert flaky.crashes == 1
