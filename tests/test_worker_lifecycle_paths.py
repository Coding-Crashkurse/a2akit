"""Coverage for intricate worker lifecycle paths.

Covers: hard cancel mid-handle (buffered-artifact survival), shutdown
interruption (broker-redelivery durability), turn-lifecycle guards, and
the blocking-wait timeout snapshot.
"""

from __future__ import annotations

import uuid

import anyio
import pytest
from a2a_pydantic.v10 import Message as V10Message
from a2a_pydantic.v10 import Part as V10Part
from a2a_pydantic.v10 import Role as V10Role
from a2a_pydantic.v10 import SendMessageRequest, Task, TaskState

from a2akit import InMemoryEventBus, InMemoryStorage, TaskContext, Worker
from a2akit.broker.memory import InMemoryBroker, InMemoryCancelRegistry
from a2akit.event_emitter import DefaultEventEmitter
from a2akit.task_manager import TaskManager
from a2akit.worker.adapter import WorkerAdapter


def _v10_message(task_id: str | None = None, text: str = "hello") -> V10Message:
    return V10Message(
        role=V10Role.role_user,
        parts=[V10Part(text=text)],
        message_id=str(uuid.uuid4()),
        task_id=task_id,
        context_id="ctx-1",
    )


async def _wait_for_state(storage, task_id: str, state: TaskState) -> None:
    while True:
        loaded = await storage.load_task(task_id)
        if loaded and loaded.status.state == state:
            return
        await anyio.sleep(0.02)


async def test_hard_cancel_mid_handle_preserves_buffered_artifacts():
    """Cancel while handle() blocks: task ends canceled AND unflushed
    artifacts survive into the canceled task (drained atomically)."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        emitter = DefaultEventEmitter(event_bus, storage)
        started = anyio.Event()

        class BlockingWorker(Worker):
            async def handle(self, ctx: TaskContext) -> None:
                # Push flush thresholds up so the artifact stays buffered.
                ctx._flush_interval = 999  # type: ignore[attr-defined]
                ctx._flush_count = 999  # type: ignore[attr-defined]
                await ctx.emit_text_artifact("partial result", artifact_id="partial")
                started.set()
                await anyio.Event().wait()  # block until hard-cancelled

        adapter = WorkerAdapter(
            BlockingWorker(), broker, storage, event_bus, cancel_reg, emitter=emitter
        )

        task_obj = await storage.create_task("ctx-1", _v10_message())
        params = SendMessageRequest(message=_v10_message(task_id=task_obj.id))

        with anyio.fail_after(10):
            async with adapter.run():
                await broker.run_task(params, is_new_task=True)
                await started.wait()
                # Verify the artifact was NOT flushed yet (still buffered).
                mid = await storage.load_task(task_obj.id)
                assert len(mid.artifacts or []) == 0
                await cancel_reg.request_cancel(task_obj.id)
                await _wait_for_state(storage, task_obj.id, TaskState.task_state_canceled)

        loaded = await storage.load_task(task_obj.id)
        assert loaded is not None
        assert loaded.status.state == TaskState.task_state_canceled
        assert any(a.artifact_id == "partial" for a in (loaded.artifacts or []))


async def test_shutdown_interruption_leaves_task_working():
    """Cancelling adapter.run() mid-handle WITHOUT a cancel request must NOT
    mark the task canceled/failed — it stays working so the broker can
    redeliver it to another worker."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        emitter = DefaultEventEmitter(event_bus, storage)
        started = anyio.Event()

        class BlockingWorker(Worker):
            async def handle(self, ctx: TaskContext) -> None:
                started.set()
                await anyio.Event().wait()  # block until shutdown

        adapter = WorkerAdapter(
            BlockingWorker(), broker, storage, event_bus, cancel_reg, emitter=emitter
        )

        task_obj = await storage.create_task("ctx-1", _v10_message())
        params = SendMessageRequest(message=_v10_message(task_id=task_obj.id))

        async def run_adapter() -> None:
            async with adapter.run():
                await anyio.Event().wait()  # hold open until cancelled

        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                tg.start_soon(run_adapter)
                await broker.run_task(params, is_new_task=True)
                await started.wait()
                await _wait_for_state(storage, task_obj.id, TaskState.task_state_working)
                # Hard shutdown — no cancel request for the task.
                tg.cancel_scope.cancel()

        loaded = await storage.load_task(task_obj.id)
        assert loaded is not None
        assert loaded.status.state == TaskState.task_state_working


async def _make_ctx():
    """Build a TaskContextImpl over real InMemory storage/event bus."""
    from a2akit.broker.memory import AnyioCancelScope
    from a2akit.worker.base import TaskContextImpl

    storage = InMemoryStorage()
    event_bus = InMemoryEventBus()
    await event_bus.__aenter__()
    emitter = DefaultEventEmitter(event_bus, storage)
    msg = _v10_message()
    task = await storage.create_task("ctx-1", msg)
    version = await storage.update_task(task.id, state=TaskState.task_state_working)
    ctx = TaskContextImpl(
        task_id=task.id,
        context_id="ctx-1",
        message_id=msg.message_id or "",
        user_text="hello",
        parts=list(msg.parts),
        metadata={},
        emitter=emitter,
        cancel_event=AnyioCancelScope(anyio.Event()),
        storage=storage,
        initial_version=version,
    )
    return ctx, storage, event_bus, task


async def test_complete_twice_raises_runtime_error():
    """A second lifecycle call in the same turn is a programming error."""
    ctx, _storage, event_bus, _task = await _make_ctx()
    try:
        with anyio.fail_after(10):
            await ctx.complete("done")
            with pytest.raises(RuntimeError, match="turn already ended"):
                await ctx.complete("again")
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_send_status_and_emit_artifact_after_complete_are_noops():
    """send_status/emit_artifact after complete() warn and do nothing."""
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        with anyio.fail_after(10):
            await ctx.complete("done")
            version = await storage.get_version(task.id)

            await ctx.send_status("late status")
            await ctx.emit_artifact(artifact_id="late", text="too late")

            assert ctx._pending_artifacts == []
            assert await storage.get_version(task.id) == version
            loaded = await storage.load_task(task.id)
            assert not any(a.artifact_id == "late" for a in (loaded.artifacts or []))
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_blocking_send_timeout_returns_in_progress_snapshot():
    """Blocking send_message against a slow worker returns the in-progress
    Task snapshot when default_blocking_timeout_s elapses, instead of hanging."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        emitter = DefaultEventEmitter(event_bus, storage)
        release = anyio.Event()

        class SlowWorker(Worker):
            async def handle(self, ctx: TaskContext) -> None:
                await release.wait()
                await ctx.complete("finally done")

        adapter = WorkerAdapter(
            SlowWorker(), broker, storage, event_bus, cancel_reg, emitter=emitter
        )
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
            default_blocking_timeout_s=0.2,
        )

        with anyio.fail_after(10):
            async with adapter.run():
                # No return_immediately → blocking semantics.
                result = await tm.send_message(SendMessageRequest(message=_v10_message()))
                assert isinstance(result, Task)
                assert result.status.state in {
                    TaskState.task_state_submitted,
                    TaskState.task_state_working,
                }
                # Let the worker finish so shutdown is clean.
                release.set()
                await _wait_for_state(storage, result.id, TaskState.task_state_completed)
