"""In-memory broker and cancel registry for single-process deployments."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import AsyncExitStack
from typing import Any, Self

import anyio
from a2a.types import MessageSendParams
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from agentserve.broker.base import (
    Broker,
    CancelRegistry,
    CancelScope,
    OperationHandle,
    TaskOperation,
    _RunTask,
)


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


class InMemoryOperationHandle(OperationHandle):
    """In-memory handle where ack is a no-op."""

    def __init__(
        self,
        op: TaskOperation,
        requeue: Callable[[TaskOperation], Coroutine[Any, Any, None]],
    ) -> None:
        self._op = op
        self._requeue = requeue

    @property
    def operation(self) -> TaskOperation:
        """Return the wrapped operation."""
        return self._op

    async def ack(self) -> None:
        """Acknowledge successful processing (no-op for in-memory)."""

    async def nack(self) -> None:
        """Re-enqueue the operation for retry."""
        await self._requeue(self._op)


class InMemoryBroker(Broker):
    """In-memory broker suitable for single-process deployments."""

    def __init__(self, ops_buffer: int = 1000) -> None:
        """Initialize buffer size and internal state."""
        self._ops_buffer = ops_buffer
        self._aexit_stack: AsyncExitStack | None = None
        self._ops_write: MemoryObjectSendStream[TaskOperation] | None = None
        self._ops_read: MemoryObjectReceiveStream[TaskOperation] | None = None

    async def __aenter__(self) -> Self:
        """Create memory streams."""
        self._aexit_stack = AsyncExitStack()
        await self._aexit_stack.__aenter__()
        self._ops_write, self._ops_read = anyio.create_memory_object_stream[
            TaskOperation
        ](max_buffer_size=self._ops_buffer)
        await self._aexit_stack.enter_async_context(self._ops_write)
        await self._aexit_stack.enter_async_context(self._ops_read)
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        """Tear down the exit stack."""
        if self._aexit_stack is not None:
            await self._aexit_stack.__aexit__(exc_type, exc_value, traceback)

    async def run_task(
        self, params: MessageSendParams, *, is_new_task: bool = False
    ) -> None:
        """Enqueue a run-task operation."""
        await self._ops_write.send(
            _RunTask(operation="run", params=params, is_new_task=is_new_task)
        )

    async def receive_task_operations(self) -> AsyncIterator[OperationHandle]:
        """Receive task operations from the internal queue."""
        return self._receive_ops()

    async def _requeue(self, op: TaskOperation) -> None:
        """Re-enqueue an operation after nack."""
        await self._ops_write.send(op)

    async def _receive_ops(self) -> AsyncIterator[OperationHandle]:
        """Yield operation handles from the internal queue."""
        async with self._ops_read:
            async for op in self._ops_read:
                yield InMemoryOperationHandle(op, self._requeue)
