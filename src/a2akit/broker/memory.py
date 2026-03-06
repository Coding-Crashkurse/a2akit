"""In-memory broker and cancel registry for single-process deployments."""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Self

import anyio

from a2akit.broker.base import (
    Broker,
    CancelRegistry,
    CancelScope,
    OperationHandle,
    TaskOperation,
    _RunTask,
)
from a2akit.config import Settings, get_settings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine
    from types import TracebackType

    from a2a.types import MessageSendParams
    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream


class AnyioCancelScope(CancelScope):
    """CancelScope backed by an anyio.Event."""

    def __init__(self, event: anyio.Event) -> None:
        self._event = event

    async def wait(self) -> None:
        """Block until cancellation is requested."""
        await self._event.wait()

    def is_set(self) -> bool:
        """Check if cancellation was requested without blocking."""
        return self._event.is_set()


class InMemoryCancelRegistry(CancelRegistry):
    """In-memory cancel registry backed by anyio events."""

    def __init__(self) -> None:
        """Initialize empty cancel event store."""
        self._cancel_events: dict[str, anyio.Event] = {}

    async def request_cancel(self, task_id: str) -> None:
        """Signal cancellation for a task."""
        self._cancel_events.setdefault(task_id, anyio.Event()).set()

    async def is_cancelled(self, task_id: str) -> bool:
        """Check if cancellation was requested for a task."""
        ev = self._cancel_events.get(task_id)
        return ev is not None and ev.is_set()

    def on_cancel(self, task_id: str) -> CancelScope:
        """Return a scope that signals when cancellation is requested."""
        return AnyioCancelScope(self._cancel_events.setdefault(task_id, anyio.Event()))

    async def cleanup(self, task_id: str) -> None:
        """Release resources for a completed task."""
        self._cancel_events.pop(task_id, None)


@dataclass
class _EnqueuedOp:
    """Internal wrapper carrying attempt count through the queue."""

    op: TaskOperation
    attempt: int = 1


class InMemoryOperationHandle(OperationHandle):
    """In-memory handle where ack is a no-op."""

    def __init__(
        self,
        op: TaskOperation,
        requeue: Callable[[_EnqueuedOp], Coroutine[Any, Any, None]],
        attempt: int = 1,
    ) -> None:
        self._op = op
        self._requeue = requeue
        self._attempt = attempt

    @property
    def operation(self) -> TaskOperation:
        """Return the wrapped operation."""
        return self._op

    @property
    def attempt(self) -> int:
        """Delivery attempt number (1-based, incremented on nack)."""
        return self._attempt

    async def ack(self) -> None:
        """Acknowledge successful processing (no-op for in-memory)."""

    async def nack(self, *, delay_seconds: float = 0) -> None:
        """Re-enqueue with incremented attempt (delay_seconds is ignored)."""
        await self._requeue(_EnqueuedOp(self._op, self._attempt + 1))


class InMemoryBroker(Broker):
    """In-memory broker suitable for single-process deployments."""

    def __init__(self, ops_buffer: int | None = None, *, settings: Settings | None = None) -> None:
        """Initialize buffer size and internal state."""
        s = settings or get_settings()
        self._ops_buffer = ops_buffer if ops_buffer is not None else s.broker_buffer
        self._aexit_stack: AsyncExitStack | None = None
        self._ops_write: MemoryObjectSendStream[_EnqueuedOp] | None = None
        self._ops_read: MemoryObjectReceiveStream[_EnqueuedOp] | None = None

    async def __aenter__(self) -> Self:
        """Create memory streams."""
        self._aexit_stack = AsyncExitStack()
        await self._aexit_stack.__aenter__()
        self._ops_write, self._ops_read = anyio.create_memory_object_stream[_EnqueuedOp](
            max_buffer_size=self._ops_buffer
        )
        await self._aexit_stack.enter_async_context(self._ops_write)
        await self._aexit_stack.enter_async_context(self._ops_read)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Tear down the exit stack."""
        if self._aexit_stack is not None:
            await self._aexit_stack.__aexit__(exc_type, exc_value, traceback)

    async def run_task(
        self,
        params: MessageSendParams,
        *,
        is_new_task: bool = False,
        request_context: dict[str, Any] | None = None,
    ) -> None:
        """Enqueue a run-task operation."""
        assert self._ops_write is not None
        await self._ops_write.send(
            _EnqueuedOp(
                _RunTask(
                    operation="run",
                    params=params,
                    is_new_task=is_new_task,
                    request_context=request_context or {},
                )
            )
        )

    async def shutdown(self) -> None:
        """Signal the broker to stop receiving operations."""
        if self._ops_write:
            await self._ops_write.aclose()

    async def receive_task_operations(self) -> AsyncIterator[OperationHandle]:
        """Yield operation handles from the internal queue.

        Stream lifecycle is managed by __aenter__/__aexit__ —
        do NOT use ``async with self._ops_read`` here (double-close).
        """
        assert self._ops_read is not None
        async for envelope in self._ops_read:
            yield InMemoryOperationHandle(envelope.op, self._requeue, envelope.attempt)

    async def _requeue(self, envelope: _EnqueuedOp) -> None:
        """Re-enqueue an operation after nack."""
        assert self._ops_write is not None
        await self._ops_write.send(envelope)
