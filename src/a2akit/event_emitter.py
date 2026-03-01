"""EventEmitter abstraction — decouples TaskContext from EventBus and Storage."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from a2a.types import Message, TaskState

    from a2akit.event_bus.base import EventBus
    from a2akit.schema import StreamEvent
    from a2akit.storage.base import ArtifactWrite, Storage

logger = logging.getLogger(__name__)


class EventEmitter(ABC):
    """Thin interface that TaskContext uses to persist state and broadcast events.

    This keeps TaskContext unaware of EventBus and Storage as separate concepts.

    **Call order contract for callers (TaskContextImpl):**

    1. ``update_task()`` — Storage write (authoritative, must succeed)
    2. ``send_event()`` — EventBus publish (best-effort, may fail)

    If step 2 fails, the state is still correct in Storage. Clients
    polling via GET will see the right state. SSE subscribers may miss
    intermediate events but will always see the final status event.
    """

    @abstractmethod
    async def update_task(
        self,
        task_id: str,
        state: TaskState | None = None,
        *,
        status_message: Message | None = None,
        artifacts: list[ArtifactWrite] | None = None,
        messages: list[Message] | None = None,
        task_metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> int:
        """Persist a task state change (and optional artifacts/messages).

        When ``state`` is ``None`` the current state is preserved.
        When ``status_message`` is provided alongside a ``state``
        transition, it is stored in ``task.status.message``.
        When ``expected_version`` is provided, it is passed through to
        Storage for optimistic concurrency control.

        Returns the new version from Storage.
        """

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
        state: TaskState | None = None,
        *,
        status_message: Message | None = None,
        artifacts: list[ArtifactWrite] | None = None,
        messages: list[Message] | None = None,
        task_metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> int:
        """Persist a task state change via storage."""
        return await self._storage.update_task(
            task_id,
            state=state,
            status_message=status_message,
            artifacts=artifacts,
            messages=messages,
            task_metadata=task_metadata,
            expected_version=expected_version,
        )

    async def send_event(self, task_id: str, event: StreamEvent) -> None:
        """Broadcast a stream event via event bus (best-effort)."""
        try:
            await self._event_bus.publish(task_id, event)
        except Exception:
            logger.exception("Failed to publish event for task %s", task_id)
