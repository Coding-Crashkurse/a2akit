"""WorkerAdapter — orchestrates broker loop, context building, and execution."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

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

from agentserve.broker import Broker, CancelRegistry, OperationHandle
from agentserve.event_bus.base import EventBus
from agentserve.storage import Storage
from agentserve.worker.base import Worker
from agentserve.worker.context_factory import ContextFactory

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._user_worker = user_worker
        self._broker = broker
        self._event_bus = event_bus
        self._cancel_registry = cancel_registry
        self._context_factory = ContextFactory(event_bus, storage)
        self._semaphore = (
            anyio.Semaphore(max_concurrent_tasks) if max_concurrent_tasks else None
        )

    @asynccontextmanager
    async def run(self) -> AsyncIterator[None]:
        """Start the broker consumption loop as a background task."""
        async with anyio.create_task_group() as tg:
            tg.start_soon(self._broker_loop)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()

    async def _broker_loop(self) -> None:
        """Continuously receive and dispatch broker operations."""
        async with anyio.create_task_group() as tg:
            async for handle in await self._broker.receive_task_operations():
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
        """Dispatch, ack on success, nack on failure."""
        try:
            await self._dispatch(handle.operation)
            await handle.ack()
        except Exception:
            try:
                await handle.nack()
            except Exception:
                logger.exception("nack itself failed")
            logger.exception("Operation failed, nacked")

    async def _dispatch(self, op: Any) -> None:
        """Route an operation to the appropriate handler."""
        if op.operation == "run":
            await self._run_task(op.params, is_new_task=op.is_new_task)

    async def _run_task(
        self, params: MessageSendParams, *, is_new_task: bool = False
    ) -> None:
        """Execute the user worker for a submitted task."""
        message = params.message
        task_id = message.task_id
        context_id = message.context_id
        if not task_id:
            raise ValueError("message.task_id is missing")

        emitter = self._context_factory.emitter
        cancel_event = self._cancel_registry.on_cancel(task_id)
        if await self._cancel_registry.is_cancelled(task_id):
            await self._mark_canceled(emitter, task_id, context_id)
            return

        ctx = await self._context_factory.build(
            message, cancel_event, is_new_task=is_new_task
        )

        await emitter.update_task(task_id, state=TaskState.working)
        await ctx.send_status()

        try:
            await self._user_worker.handle(ctx)
            if not ctx.turn_ended:
                await ctx.fail(
                    "Worker returned without calling a lifecycle method "
                    "(complete/fail/reject/respond/request_input/request_auth)"
                )
        except anyio.get_cancelled_exc_class():
            await self._mark_canceled(emitter, task_id, context_id)
        except Exception as exc:
            logger.exception("Worker error for task %s", task_id)
            await self._mark_failed(emitter, task_id, context_id, str(exc))
        finally:
            await self._event_bus.cleanup(task_id)
            await self._cancel_registry.cleanup(task_id)

    @staticmethod
    async def _mark_canceled(emitter, task_id: str, context_id: str | None) -> None:
        """Persist canceled state and emit a final status event."""
        await emitter.update_task(task_id, state=TaskState.canceled)
        status = TaskStatus(
            state=TaskState.canceled, timestamp=datetime.now(UTC).isoformat()
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

    @staticmethod
    async def _mark_failed(
        emitter, task_id: str, context_id: str | None, reason: str
    ) -> None:
        """Persist failed state and emit a final status event with the error."""
        await emitter.update_task(task_id, state=TaskState.failed)
        status = TaskStatus(
            state=TaskState.failed,
            timestamp=datetime.now(UTC).isoformat(),
            message=Message(
                role=Role.agent,
                parts=[Part(TextPart(text=reason))],
                message_id=str(uuid.uuid4()),
            ),
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
