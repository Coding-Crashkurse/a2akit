"""In-memory event bus for single-process deployments."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import anyio
from a2a.types import TaskStatusUpdateEvent
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)

from a2akit.event_bus.base import EventBus
from a2akit.schema import StreamEvent


class InMemoryEventBus(EventBus):
    """In-memory fan-out event bus backed by anyio memory streams."""

    def __init__(self, event_buffer: int = 200) -> None:
        """Initialize buffer size and internal state."""
        self._event_buffer = event_buffer
        self._event_subscribers: dict[
            str, list[MemoryObjectSendStream[StreamEvent]]
        ] = {}
        self._subscriber_lock: anyio.Lock | None = None

    async def __aenter__(self):
        """Acquire the subscriber lock."""
        self._subscriber_lock = anyio.Lock()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        """Tear down (no-op for in-memory)."""

    async def publish(self, task_id: str, event: StreamEvent) -> str | None:
        """Fan out a stream event to all subscribers of a task."""
        async with self._subscriber_lock:
            subscribers = self._event_subscribers.get(task_id, [])
            if not subscribers:
                return None
            alive: list[MemoryObjectSendStream[StreamEvent]] = []
            for s in subscribers:
                try:
                    await s.send(event)
                    alive.append(s)
                except (anyio.ClosedResourceError, anyio.BrokenResourceError):
                    pass
            if alive:
                self._event_subscribers[task_id] = alive
            else:
                self._event_subscribers.pop(task_id, None)
        return None

    @asynccontextmanager
    async def subscribe(
        self, task_id: str, *, after_event_id: str | None = None
    ) -> AsyncIterator[AsyncIterator[StreamEvent]]:
        """Subscribe to stream events. Cleanup is guaranteed by context manager."""
        send_stream, recv_stream = anyio.create_memory_object_stream[StreamEvent](
            max_buffer_size=self._event_buffer
        )
        async with self._subscriber_lock:
            self._event_subscribers.setdefault(task_id, []).append(send_stream)
        try:
            yield self._iter_events(recv_stream)
        finally:
            async with self._subscriber_lock:
                lst = self._event_subscribers.get(task_id)
                if lst:
                    with suppress(ValueError):
                        lst.remove(send_stream)
                    if not lst:
                        self._event_subscribers.pop(task_id, None)
            await send_stream.aclose()
            await recv_stream.aclose()

    async def _iter_events(
        self,
        recv_stream: MemoryObjectReceiveStream[StreamEvent],
    ) -> AsyncIterator[StreamEvent]:
        """Yield events until a final status event arrives."""
        async for ev in recv_stream:
            yield ev
            if isinstance(ev, TaskStatusUpdateEvent) and ev.final:
                break

    async def cleanup(self, task_id: str) -> None:
        """Remove subscriber state for a completed task."""
        async with self._subscriber_lock:
            self._event_subscribers.pop(task_id, None)
