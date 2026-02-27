"""EventEmitter abstraction — decouples TaskContext from EventBus and Storage."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from a2a.types import Artifact, Message, Task, TaskState

from agentserve.event_bus.base import EventBus
from agentserve.schema import StreamEvent
from agentserve.storage.base import Storage

logger = logging.getLogger(__name__)


class EventEmitter(ABC):
    """Thin interface that TaskContext uses to persist state and broadcast events.

    This keeps TaskContext unaware of EventBus and Storage as separate concepts.
    """

    @abstractmethod
    async def update_task(
        self,
        task_id: str,
        state: TaskState,
        *,
        artifacts: list[Artifact] | None = None,
        messages: list[Message] | None = None,
        append_artifact: bool = False,
    ) -> Task:
        """Persist a task state change (and optional artifacts/messages)."""

    @abstractmethod
    async def send_event(self, task_id: str, event: StreamEvent) -> None:
        """Broadcast a stream event to all subscribers of a task."""


class DefaultEventEmitter(EventEmitter):
    """Default implementation that delegates to an EventBus and Storage pair.

    Storage write is authoritative. EventBus failure is logged but not raised,
    providing at-least-once delivery semantics for Storage and best-effort for EventBus.
    """

    def __init__(self, event_bus: EventBus, storage: Storage) -> None:
        self._event_bus = event_bus
        self._storage = storage

    async def update_task(
        self,
        task_id: str,
        state: TaskState,
        *,
        artifacts: list[Artifact] | None = None,
        messages: list[Message] | None = None,
        append_artifact: bool = False,
    ) -> Task:
        """Persist a task state change via storage."""
        return await self._storage.update_task(
            task_id,
            state=state,
            artifacts=artifacts,
            messages=messages,
            append_artifact=append_artifact,
        )

    async def send_event(self, task_id: str, event: StreamEvent) -> None:
        """Broadcast a stream event via event bus (best-effort)."""
        try:
            await self._event_bus.publish(task_id, event)
        except Exception:
            logger.exception("Failed to publish event for task %s", task_id)
