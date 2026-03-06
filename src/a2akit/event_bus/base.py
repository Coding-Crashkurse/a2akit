"""EventBus ABC — 1:N event fan-out for task stream events."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from contextlib import AbstractAsyncContextManager

    from a2akit.schema import StreamEvent


class EventBus(ABC):
    """Abstract event bus for broadcasting stream events to subscribers."""

    @abstractmethod
    async def publish(self, task_id: str, event: StreamEvent) -> str | None:
        """Publish a stream event to all subscribers of a task.

        Returns an event ID if the backend supports it (e.g. Redis
        Streams, Kafka), or ``None`` for backends that don't assign
        IDs (e.g. InMemory).

        Implementations MUST deliver events in the order they were
        published for a given ``task_id``.
        """

    @abstractmethod
    def subscribe(
        self, task_id: str, *, after_event_id: str | None = None
    ) -> AbstractAsyncContextManager[AsyncIterator[StreamEvent]]:
        """Subscribe to stream events for a task.

        MUST be used as an async context manager:

            async with event_bus.subscribe(task_id) as stream:
                async for event in stream:
                    ...

        The context manager guarantees cleanup of subscriber resources
        (e.g. Redis UNSUBSCRIBE, RabbitMQ consumer de-registration)
        regardless of how the consumer exits (normal, exception, cancel).

        When ``after_event_id`` is provided, backends that support
        replay (e.g. Redis Streams) deliver events published after
        that ID.  InMemory ignores this parameter.

        Implementations MUST yield events in the order they were
        published (per A2A spec §3.5.2).
        """
        ...

    @abstractmethod
    async def cleanup(self, task_id: str) -> None:
        """Release subscriber resources for a completed task.

        MUST be idempotent. Multiple calls with the same task_id
        MUST NOT raise and MUST NOT affect resources for other tasks.

        Called by WorkerAdapter at the end of normal task processing
        and by TaskManager._force_cancel_after when the worker does
        not cooperate within the cancel timeout.  No other component
        may call this method.

        Backends MUST implement this to avoid resource leaks
        (e.g. Redis UNSUBSCRIBE, key cleanup).
        """

    async def __aenter__(self) -> Self:
        """Enter the async context manager."""
        return self

    async def __aexit__(  # noqa: B027
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        """Exit the async context manager."""
