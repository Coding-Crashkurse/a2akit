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
        """Continuously receive and dispatch broker operations.

        When a concurrency limit is configured, the semaphore is acquired
        BEFORE pulling the next message from the broker. This applies
        backpressure to the queue itself — unacknowledged messages stay in
        the broker (where other workers can claim them) instead of being
        pulled into local memory and parked behind a semaphore. Essential
        for horizontal scaling with Redis Streams (XREADGROUP) and for
        avoiding OOM with large backlogs.
        """
        async with anyio.create_task_group() as tg:
            async for handle in self._broker.receive_task_operations():
                if self._semaphore is not None:
                    await self._semaphore.acquire()
                tg.start_soon(self._handle_op, handle)

    async def _handle_op(self, handle: OperationHandle) -> None:
        """Execute a single operation with ack/nack handling.

        Catches all exceptions so a single failed operation never tears
        down the entire TaskGroup and its sibling tasks. The semaphore
        (if configured) is acquired by ``_broker_loop`` before dispatch
        and released here in a ``finally`` so the slot is freed even if
        the handler raises.
        """
        try:
            await self._handle_op_inner(handle)
        except Exception:
            logger.exception("Unrecoverable error in operation handler")
        finally:
            if self._semaphore is not None:
                self._semaphore.release()

    async def _handle_op_inner(self, handle: OperationHandle) -> None:
        """Dispatch, ack on success, nack for retry or mark failed on error.

        Uses ``handle.attempt`` to decide between retry (nack) and
        terminal failure (ack + mark failed).  Queue backends with
        retry tracking get up to ``max_retries`` attempts with
        exponential back-off.
        """
        # Pre-check: poison pills already in DLQ — just mark failed, don't dispatch
        if handle.attempt > self._max_retries:
            logger.error(
                "Skipping dispatch for poison pill (attempt %d/%d)",
                handle.attempt,
                self._max_retries,
            )
            try:
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
                        "Task repeatedly crashed worker processes",
                    )
            except Exception:
                logger.exception("Failed to mark poison pill task as failed")
            finally:
                try:
                    await handle.ack()
                except Exception:
                    logger.warning("Failed to ack poison pill message")
            return

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
                try:
                    await handle.nack(delay_seconds=min(handle.attempt * 2, 30))
                except Exception:
                    logger.warning("nack failed, falling through to mark_failed")
                else:
                    return

            # Max retries reached: mark failed THEN ack (ensures DB write
            # lands before the message is removed from the queue).
            try:
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
            finally:
                if not acked:
                    try:
                        await handle.ack()
                    except Exception:
                        logger.exception("Failed to ack after max retries")

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
        from contextlib import nullcontext

        from a2akit.telemetry._instruments import get_tracer
        from a2akit.telemetry._semantic import (
            ATTR_CONTEXT_ID,
            ATTR_IS_NEW_TASK,
            ATTR_TASK_ID,
            ATTR_TASK_STATE,
            ATTR_WORKER_CLASS,
            EVENT_CANCEL_REQUESTED,
            SPAN_TASK_PROCESS,
        )

        message = params.message
        task_id = message.task_id
        context_id = message.context_id
        if not task_id:
            raise ValueError("message.task_id is missing")

        tracer = get_tracer()
        span_ctx: Any = nullcontext()
        if tracer is not None:
            from opentelemetry.trace import SpanKind

            span_ctx = tracer.start_as_current_span(
                SPAN_TASK_PROCESS,
                kind=SpanKind.INTERNAL,
                attributes={
                    ATTR_TASK_ID: task_id,
                    ATTR_CONTEXT_ID: context_id or "",
                    ATTR_IS_NEW_TASK: is_new_task,
                    ATTR_WORKER_CLASS: type(self._user_worker).__name__,
                },
            )

        emitter = self._emitter
        cancel_event = self._cancel_registry.on_cancel(task_id)
        with span_ctx as span:
            try:
                if await self._cancel_registry.is_cancelled(task_id):
                    current = await self._storage.load_task(task_id)
                    if not current or current.status.state not in TERMINAL_STATES:
                        await self._mark_canceled(
                            self._storage, self._emitter, task_id, context_id
                        )
                    if span:
                        span.add_event(EVENT_CANCEL_REQUESTED)
                    return

                cfg = getattr(params, "configuration", None)
                ctx = await self._context_factory.build(
                    message,
                    cancel_event,
                    is_new_task=is_new_task,
                    request_context=request_context,
                    configuration=cfg,
                    deferred_storage=cfg is not None and not getattr(cfg, "blocking", False),
                )

                try:
                    working_version = await emitter.update_task(task_id, state=TaskState.working)
                except TaskTerminalStateError:
                    return
                except ConcurrencyError:
                    # Another writer (force-cancel, follow-up submit) raced us
                    # on the submitted → working transition. Storage backends
                    # without transparent retry (SQL) surface this as OCC
                    # conflict. Don't mark the task as failed — the other
                    # writer has already advanced the task, cleanup in the
                    # finally block handles the rest.
                    logger.info("Task %s working-transition lost to concurrent writer", task_id)
                    return

                ctx._version = working_version
                await ctx.send_status()

                try:
                    async with anyio.create_task_group() as tg:

                        async def _cancel_watcher() -> None:
                            await cancel_event.wait()
                            tg.cancel_scope.cancel()

                        tg.start_soon(_cancel_watcher)
                        await self._user_worker.handle(ctx)
                        if not ctx.turn_ended:
                            if cancel_event.is_set():
                                # Cooperative cancel: worker checked is_cancelled and returned.
                                # Don't mark as failed — mark as canceled below.
                                pass
                            else:
                                await ctx.fail(
                                    "Worker returned without calling a lifecycle method "
                                    "(complete/fail/reject/respond/request_input/request_auth)"
                                )
                        tg.cancel_scope.cancel()

                    if cancel_event.is_set() and not ctx.turn_ended:
                        pending = await self._drain_pending_artifacts(ctx, task_id)
                        await self._mark_canceled(
                            self._storage,
                            self._emitter,
                            task_id,
                            context_id,
                            artifacts=pending,
                        )
                        if span:
                            span.add_event(EVENT_CANCEL_REQUESTED)
                except anyio.get_cancelled_exc_class():
                    with anyio.CancelScope(shield=True):
                        if cancel_event.is_set():
                            # User-initiated cancel — mark as canceled
                            if span:
                                span.add_event(EVENT_CANCEL_REQUESTED)
                            pending = await self._drain_pending_artifacts(ctx, task_id)
                            await self._mark_canceled(
                                self._storage,
                                self._emitter,
                                task_id,
                                context_id,
                                artifacts=pending,
                            )
                        else:
                            # Server shutdown — do NOT mark as canceled.
                            # Let the broker NACK so another worker picks it up.
                            logger.info(
                                "Task %s interrupted by shutdown, will be retried", task_id
                            )
                            raise
                except TaskTerminalStateError:
                    logger.info("Task %s reached terminal state during processing", task_id)
                    # Drain buffered artifacts that were already sent via SSE
                    # but not yet flushed to storage.  Write them as an
                    # artifact-only update (state=None bypasses the terminal
                    # guard) so polling clients see them too.
                    pending = await self._drain_pending_artifacts(ctx, task_id)
                    if pending:
                        try:
                            await emitter.update_task(task_id, artifacts=pending)
                        except Exception:
                            logger.warning(
                                "Failed to flush pending artifacts for terminal task %s",
                                task_id,
                            )
                except Exception as exc:
                    if ctx.turn_ended:
                        # Exception after a successful lifecycle call (e.g.
                        # cleanup code after complete()).  The task state is
                        # correct — log it but don't pollute telemetry with
                        # a false error or attempt a redundant _mark_failed.
                        logger.warning(
                            "Post-lifecycle exception for task %s (ignored, "
                            "task already terminal): %s",
                            task_id,
                            exc,
                        )
                        if span:
                            span.add_event(
                                "post_lifecycle_exception",
                                {"exception.message": str(exc)},
                            )
                    else:
                        logger.exception("Worker error for task %s", task_id)
                        if span:
                            span.record_exception(exc)
                            from opentelemetry.trace import StatusCode

                            span.set_status(StatusCode.ERROR, str(exc))
                        pending = await self._drain_pending_artifacts(ctx, task_id)
                        await self._mark_failed(
                            emitter,
                            self._storage,
                            task_id,
                            context_id,
                            str(exc),
                            artifacts=pending,
                        )
                else:
                    if span:
                        loaded = await self._storage.load_task(task_id)
                        if loaded and loaded.status:
                            span.set_attribute(ATTR_TASK_STATE, loaded.status.state.value)
                        from opentelemetry.trace import StatusCode

                        span.set_status(StatusCode.OK)
            finally:
                with anyio.CancelScope(shield=True):
                    # Only clean up event bus and cancel registry for terminal
                    # tasks. Non-terminal states (input_required, auth_required)
                    # need the replay buffer for client reconnects, and
                    # shutdown-retried tasks need the cancel key intact.
                    try:
                        current = await self._storage.load_task(task_id)
                    except Exception:
                        logger.warning("Failed to load task %s during cleanup", task_id)
                        current = None
                    # Deleted tasks (current is None) are treated as terminal for
                    # cleanup purposes — there are no subscribers or cancel keys
                    # to preserve, and skipping cleanup would leak event-bus
                    # resources (e.g. Redis TTL-refresh) and cancel-registry keys.
                    is_terminal = current is None or bool(current.status.state in TERMINAL_STATES)
                    if is_terminal:
                        try:
                            await self._event_bus.cleanup(task_id)
                        except Exception:
                            logger.exception("event_bus cleanup failed for %s", task_id)
                    # CancelRegistry cleanup runs on EVERY turn (not just terminal).
                    # The cancel scope holds a Redis Pub/Sub connection that must
                    # be released even for input_required/auth_required pauses.
                    # On non-terminal turns pass release_key=False so the cancel
                    # KEY itself is preserved — otherwise a request_cancel that
                    # arrives between turns would be silently lost and the user
                    # would have to wait for the force-cancel timeout.
                    try:
                        await self._cancel_registry.cleanup(task_id, release_key=is_terminal)
                    except Exception:
                        logger.exception("cancel_registry cleanup failed for %s", task_id)

    @staticmethod
    async def _drain_pending_artifacts(ctx: Any, task_id: str) -> list[Any]:
        """Take all buffered artifacts out of ``ctx`` so they can be
        included atomically with the terminal state write.

        We deliberately do NOT call ``ctx._flush_artifacts()`` here:
        flushing writes artifacts in a separate transaction, which is
        racy against the force-cancel path (the flush may hit
        ``ConcurrencyError``, re-buffer, and return silently — meaning
        the subsequent ``_mark_failed``/``_mark_canceled`` would miss
        them and the artifacts would be lost for polling clients).
        Instead, we pull them out of the buffer and pass them through
        to the terminal write so the state change and the artifacts
        land in a single storage transaction.
        """
        pending = ctx._pending_artifacts
        ctx._pending_artifacts = []
        return list(pending)

    @staticmethod
    async def _mark_canceled(
        storage: Storage,
        emitter: EventEmitter,
        task_id: str,
        context_id: str | None,
        *,
        artifacts: list[Any] | None = None,
    ) -> None:
        """Persist canceled state (with reason) and emit a final status event."""
        await cancel_task_in_storage(storage, emitter, task_id, context_id, artifacts=artifacts)

    @staticmethod
    async def _mark_failed(
        emitter: EventEmitter,
        storage: Storage,
        task_id: str,
        context_id: str | None,
        reason: str,
        *,
        artifacts: list[Any] | None = None,
    ) -> None:
        """Persist failed state (with reason) and emit a final status event.

        On ``ConcurrencyError``, re-loads once and retries if the task is
        still non-terminal — mirrors ``cancel_task_in_storage`` semantics.

        ``artifacts`` are written atomically with the state transition so
        buffered artifacts from a mid-run failure/cancel are not lost.
        """
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
                artifacts=artifacts or None,
                expected_version=version,
            )
        except TaskTerminalStateError:
            # State transition rejected — task already terminal.
            # Write artifacts separately (state=None bypasses guard)
            # so buffered artifacts aren't silently lost.
            if artifacts:
                try:
                    await emitter.update_task(task_id, artifacts=artifacts)
                except Exception:
                    logger.warning("Artifact-only fallback failed for task %s", task_id)
            return
        except ConcurrencyError:
            # Another writer changed the task — retry once if still non-terminal.
            # Capture version before load to maintain TOCTOU-safe ordering.
            version = await storage.get_version(task_id)
            task = await storage.load_task(task_id)
            if task is None or task.status.state in TERMINAL_STATES:
                # Task is terminal — write artifacts separately.
                if artifacts:
                    try:
                        await emitter.update_task(task_id, artifacts=artifacts)
                    except Exception:
                        logger.warning("Artifact-only fallback failed for task %s", task_id)
                return
            try:
                await emitter.update_task(
                    task_id,
                    state=TaskState.failed,
                    status_message=error_message,
                    messages=[error_message],
                    artifacts=artifacts or None,
                    expected_version=version,
                )
            except (ConcurrencyError, TaskTerminalStateError):
                logger.warning(
                    "mark_failed retry failed for task %s, task may have been modified", task_id
                )
                # Last resort: write artifacts even if state transition failed.
                if artifacts:
                    try:
                        await emitter.update_task(task_id, artifacts=artifacts)
                    except Exception:
                        logger.warning("Artifact-only fallback failed for task %s", task_id)
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
