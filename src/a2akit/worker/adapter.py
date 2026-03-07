"""WorkerAdapter — orchestrates broker loop, context building, and execution."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import anyio
from a2a.types import (
    Message,
    MessageSendParams,
    Part,
    Role,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)

from a2akit.cancel import cancel_task_in_storage
from a2akit.event_emitter import DefaultEventEmitter, EventEmitter
from a2akit.storage.base import TERMINAL_STATES, ConcurrencyError, TaskTerminalStateError
from a2akit.worker.context_factory import ContextFactory

if TYPE_CHECKING:
    from a2akit.broker import Broker, CancelRegistry, OperationHandle
    from a2akit.dependencies import DependencyContainer
    from a2akit.event_bus.base import EventBus
    from a2akit.storage import Storage
    from a2akit.worker.base import Worker

logger = logging.getLogger(__name__)

TaskLockFactory = Callable[[str], Any]
"""Callable that takes a task_id and returns an async context manager.

Used for distributed task-level locking when the broker cannot
guarantee serialization at the queue level.  Example for Redis::

    def redis_task_lock(task_id: str):
        return redis_client.lock(f"a2a:task:{task_id}", timeout=300)
"""


class WorkerAdapter:
    """Bridges a user Worker to the internal broker loop.

    Delegates context building to ContextFactory — itself only
    orchestrates the lifecycle: receive ops, run/cancel, handle errors.
    """

    def __init__(
        self,
        user_worker: Worker,
        broker: Broker,
        storage: Storage,
        event_bus: EventBus,
        cancel_registry: CancelRegistry,
        *,
        max_concurrent_tasks: int | None = None,
        max_retries: int = 3,
        task_lock_factory: TaskLockFactory | None = None,
        emitter: EventEmitter | None = None,
        deps: DependencyContainer | None = None,
    ) -> None:
        self._user_worker = user_worker
        self._broker = broker
        self._storage = storage
        self._event_bus = event_bus
        self._cancel_registry = cancel_registry
        self._emitter = emitter or DefaultEventEmitter(event_bus, storage)
        self._context_factory = ContextFactory(self._emitter, storage, deps=deps)
        self._max_retries = max_retries
        self._semaphore = anyio.Semaphore(max_concurrent_tasks) if max_concurrent_tasks else None
        self._task_lock_factory = task_lock_factory

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Start the broker consumption loop as a background task."""
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._broker_loop)
            try:
                yield
            finally:
                await self._broker.shutdown()
                tg.cancel_scope.cancel()

    async def _broker_loop(self) -> None:
        """Continuously receive and dispatch broker operations."""
        async with anyio.create_task_group() as tg:
            async for handle in self._broker.receive_task_operations():
                tg.start_soon(self._handle_op, handle)

    async def _handle_op(self, handle: OperationHandle) -> None:
        """Execute a single operation with ack/nack handling.

        Catches all exceptions so a single failed operation never tears
        down the entire TaskGroup and its sibling tasks.
        """
        try:
            if self._semaphore:
                async with self._semaphore:
                    await self._handle_op_inner(handle)
            else:
                await self._handle_op_inner(handle)
        except BaseException:
            logger.exception("Unrecoverable error in operation handler")

    async def _handle_op_inner(self, handle: OperationHandle) -> None:
        """Dispatch, ack on success, nack for retry or mark failed on error.

        Uses ``handle.attempt`` to decide between retry (nack) and
        terminal failure (ack + mark failed).  Queue backends with
        retry tracking get up to ``max_retries`` attempts with
        exponential back-off.
        """
        acked = False
        try:
            await self._dispatch(handle.operation)
            await handle.ack()
            acked = True
        except Exception:
            logger.exception(
                "Operation failed (attempt %d/%d)",
                handle.attempt,
                self._max_retries,
            )
            if handle.attempt < self._max_retries:
                await handle.nack(delay_seconds=min(handle.attempt * 2, 30))
                return

            # Max retries reached: ack + mark failed
            try:
                if not acked:
                    await handle.ack()
                op = handle.operation
                params = getattr(op, "params", None)
                msg = getattr(params, "message", None) if params else None
                task_id = getattr(msg, "task_id", None) if msg else None
                context_id = getattr(msg, "context_id", None) if msg else None
                if task_id:
                    await self._mark_failed(
                        self._emitter,
                        self._storage,
                        task_id,
                        context_id,
                        "Worker failed to process task",
                    )
            except Exception:
                logger.exception("Failed to mark task as failed after error")

    async def _dispatch(self, op: Any) -> None:
        """Route an operation to the appropriate handler."""
        if op.operation == "run":
            await self._run_task(
                op.params,
                is_new_task=op.is_new_task,
                request_context=getattr(op, "request_context", None) or {},
            )

    async def _run_task(
        self,
        params: MessageSendParams,
        *,
        is_new_task: bool = False,
        request_context: dict[str, Any] | None = None,
    ) -> None:
        """Execute the user worker for a submitted task.

        When ``task_lock_factory`` is configured, acquires a per-task
        lock before execution to prevent concurrent processing of the
        same task_id by multiple workers.
        """
        message = params.message
        task_id = message.task_id
        if not task_id:
            raise ValueError("message.task_id is missing")

        if self._task_lock_factory:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(self._task_lock_factory(task_id))
                await self._run_task_inner(
                    params, is_new_task=is_new_task, request_context=request_context
                )
        else:
            await self._run_task_inner(
                params, is_new_task=is_new_task, request_context=request_context
            )

    async def _run_task_inner(
        self,
        params: MessageSendParams,
        *,
        is_new_task: bool = False,
        request_context: dict[str, Any] | None = None,
    ) -> None:
        """Inner task execution — called with or without lock."""
        message = params.message
        task_id = message.task_id
        context_id = message.context_id
        if not task_id:
            raise ValueError("message.task_id is missing")

        emitter = self._emitter
        cancel_event = self._cancel_registry.on_cancel(task_id)
        try:
            if await self._cancel_registry.is_cancelled(task_id):
                # Check if the task is already terminal (e.g. TaskManager's
                # instant cancel on submitted).  If so, skip _mark_canceled
                # to avoid double-writing.
                current = await self._storage.load_task(task_id)
                if not current or current.status.state not in TERMINAL_STATES:
                    await self._mark_canceled(self._storage, self._emitter, task_id, context_id)
                return

            ctx = await self._context_factory.build(
                message,
                cancel_event,
                is_new_task=is_new_task,
                request_context=request_context,
            )

            try:
                working_version = await emitter.update_task(task_id, state=TaskState.working)
            except TaskTerminalStateError:
                # Task was already terminated (e.g., canceled before the
                # worker picked it up).  Nothing to do — just clean up.
                return

            # Seed context with the version from the working transition
            # so subsequent _versioned_update calls use OCC.
            ctx._version = working_version

            await ctx.send_status()

            try:
                await self._user_worker.handle(ctx)
                if not ctx.turn_ended:
                    await ctx.fail(
                        "Worker returned without calling a lifecycle method "
                        "(complete/fail/reject/respond/request_input/request_auth)"
                    )
            except anyio.get_cancelled_exc_class():
                await self._mark_canceled(self._storage, self._emitter, task_id, context_id)
            except TaskTerminalStateError:
                # Task reached terminal state during processing
                # (e.g., force-cancel wrote 'canceled' concurrently).
                # Nothing to do — cleanup runs in finally.
                logger.info("Task %s reached terminal state during processing", task_id)
            except Exception as exc:
                logger.exception("Worker error for task %s", task_id)
                await self._mark_failed(emitter, self._storage, task_id, context_id, str(exc))
        finally:
            await self._event_bus.cleanup(task_id)
            await self._cancel_registry.cleanup(task_id)

    @staticmethod
    async def _mark_canceled(
        storage: Storage,
        emitter: EventEmitter,
        task_id: str,
        context_id: str | None,
    ) -> None:
        """Persist canceled state (with reason) and emit a final status event."""
        await cancel_task_in_storage(storage, emitter, task_id, context_id)

    @staticmethod
    async def _mark_failed(
        emitter: EventEmitter, storage: Storage, task_id: str, context_id: str | None, reason: str
    ) -> None:
        """Persist failed state (with reason) and emit a final status event."""
        error_message = Message(
            role=Role.agent,
            parts=[Part(TextPart(text=reason))],
            message_id=str(uuid.uuid4()),
            task_id=task_id,
            context_id=context_id,
        )
        version = await storage.get_version(task_id)
        try:
            await emitter.update_task(
                task_id,
                state=TaskState.failed,
                status_message=error_message,
                messages=[error_message],
                expected_version=version,
            )
        except (ConcurrencyError, TaskTerminalStateError):
            return
        status = TaskStatus(
            state=TaskState.failed,
            timestamp=datetime.now(UTC).isoformat(),
            message=error_message,
        )
        await emitter.send_event(
            task_id,
            TaskStatusUpdateEvent(
                kind="status-update",
                task_id=task_id,
                context_id=context_id,
                status=status,
                final=True,
            ),
        )
