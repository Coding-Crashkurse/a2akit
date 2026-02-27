"""ContextFactory — builds TaskContextImpl from A2A Message objects."""

from __future__ import annotations

from typing import Any

from a2a.types import Artifact as A2AArtifact
from a2a.types import Message, Part, TextPart

from agentserve.broker.base import CancelScope
from agentserve.event_bus.base import EventBus
from agentserve.event_emitter import DefaultEventEmitter, EventEmitter
from agentserve.storage import Storage
from agentserve.worker.base import (
    HistoryMessage,
    PreviousArtifact,
    TaskContextImpl,
)


class ContextFactory:
    """Translates an A2A Message into a clean TaskContextImpl."""

    def __init__(self, event_bus: EventBus, storage: Storage) -> None:
        self._event_bus = event_bus
        self._storage = storage
        self._emitter = DefaultEventEmitter(event_bus, storage)

    @property
    def emitter(self) -> EventEmitter:
        """Return the shared EventEmitter instance."""
        return self._emitter

    async def build(
        self,
        message: Message,
        cancel_event: CancelScope,
        *,
        is_new_task: bool = False,
    ) -> TaskContextImpl:
        """Construct a TaskContextImpl from a broker message."""
        user_text = self._extract_text(message.parts)

        history: list[HistoryMessage] = []
        previous_artifacts: list[PreviousArtifact] = []

        if not is_new_task and message.task_id:
            task = await self._storage.load_task(message.task_id)
            if task:
                history = self._convert_history(
                    task.history or [], message.message_id or ""
                )
                previous_artifacts = self._convert_artifacts(task.artifacts or [])

        return TaskContextImpl(
            task_id=message.task_id,
            context_id=message.context_id,
            message_id=message.message_id or "",
            user_text=user_text,
            parts=message.parts,
            metadata=message.metadata or {},
            emitter=self._emitter,
            cancel_event=cancel_event,
            storage=self._storage,
            history=history,
            previous_artifacts=previous_artifacts,
        )

    @staticmethod
    def _extract_text(parts: list[Part]) -> str:
        """Join all text parts of a message into a single string."""
        return "\n".join(
            part.root.text
            for part in parts
            if isinstance(part.root, TextPart) and part.root.text
        )

    @staticmethod
    def _convert_history(
        messages: list[Any], current_message_id: str
    ) -> list[HistoryMessage]:
        """Convert A2A Messages to HistoryMessage wrappers, excluding current."""
        result: list[HistoryMessage] = []
        for msg in messages:
            msg_id = getattr(msg, "message_id", "") or ""
            if msg_id == current_message_id and current_message_id:
                continue
            text_parts = []
            for part in getattr(msg, "parts", []):
                root = getattr(part, "root", part)
                if isinstance(root, TextPart) and root.text:
                    text_parts.append(root.text)
            result.append(
                HistoryMessage(
                    role=getattr(msg.role, "value", str(msg.role)) if msg.role else "",
                    text="\n".join(text_parts),
                    parts=list(getattr(msg, "parts", [])),
                    message_id=msg_id,
                )
            )
        return result

    @staticmethod
    def _convert_artifacts(artifacts: list[A2AArtifact]) -> list[PreviousArtifact]:
        """Convert A2A Artifacts to PreviousArtifact wrappers."""
        return [
            PreviousArtifact(
                artifact_id=a.artifact_id,
                name=getattr(a, "name", None),
                parts=list(a.parts) if a.parts else [],
            )
            for a in artifacts
        ]
