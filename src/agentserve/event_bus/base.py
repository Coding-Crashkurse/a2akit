"""EventBus ABC — 1:N event fan-out for task stream events."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Self

from agentserve.schema import StreamEvent


class EventBus(ABC):
    """Abstract event bus for broadcasting stream events to subscribers."""

    @abstractmethod
    async def publish(self, task_id: str, event: StreamEvent) -> None:
        """Publish a stream event to all subscribers of a task."""

    @abstractmethod
    async def subscribe(self, task_id: str) -> AsyncIterator[StreamEvent]:
        """Subscribe to stream events for a task. Setup may be async."""

    async def cleanup(self, task_id: str) -> None:
        """Release subscriber resources for a completed task.

        Default is a no-op; backends override as needed.
        """

    async def __aenter__(self) -> Self:
        """Enter the async context manager."""
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        """Exit the async context manager."""
