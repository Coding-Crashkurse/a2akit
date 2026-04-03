"""Result wrappers for A2A client responses."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from a2a.types import (
    Artifact,
    DataPart,
    Message,
    Task,
    TaskArtifactUpdateEvent,
    TaskStatusUpdateEvent,
    TextPart,
)


def _extract_text_from_parts(parts: list[Any]) -> str | None:
    """Extract and join text from Part objects."""
    texts: list[str] = []
    for p in parts:
        inner = p.root if hasattr(p, "root") else p
        if isinstance(inner, TextPart):
            texts.append(inner.text)
    return "".join(texts) if texts else None


def _extract_data_from_parts(parts: list[Any]) -> dict[str, Any] | None:
    """Extract first DataPart data from Part objects."""
    for p in parts:
        inner = p.root if hasattr(p, "root") else p
        if isinstance(inner, DataPart):
            return inner.data
    return None


@dataclass(frozen=True)
class ArtifactInfo:
    """Read-only wrapper for an individual artifact."""

    artifact_id: str
    name: str | None
    description: str | None
    text: str | None
    data: dict[str, Any] | None
    parts: list[Any]
    metadata: dict[str, Any] | None

    @classmethod
    def from_artifact(cls, artifact: Artifact) -> ArtifactInfo:
        """Build ArtifactInfo from an Artifact object."""
        raw_parts = list(artifact.parts) if artifact.parts else []
        return cls(
            artifact_id=artifact.artifact_id,
            name=artifact.name,
            description=artifact.description,
            text=_extract_text_from_parts(raw_parts),
            data=_extract_data_from_parts(raw_parts),
            parts=raw_parts,
            metadata=dict(artifact.metadata) if artifact.metadata else None,
        )


@dataclass(frozen=True)
class ClientResult:
    """Primary return type for non-streaming client operations."""

    task_id: str
    context_id: str | None
    state: str

    text: str | None
    data: dict[str, Any] | None
    artifacts: list[ArtifactInfo]

    raw_task: Task | None
    raw_message: Message | None

    @property
    def completed(self) -> bool:
        return self.state == "completed"

    @property
    def failed(self) -> bool:
        return self.state == "failed"

    @property
    def input_required(self) -> bool:
        return self.state == "input-required"

    @property
    def auth_required(self) -> bool:
        return self.state == "auth-required"

    @property
    def canceled(self) -> bool:
        return self.state == "canceled"

    @property
    def rejected(self) -> bool:
        return self.state == "rejected"

    @property
    def is_terminal(self) -> bool:
        return self.state in ("completed", "failed", "canceled", "rejected")

    @classmethod
    def from_task(cls, task: Task) -> ClientResult:
        """Build ClientResult from a Task object."""
        artifacts_info: list[ArtifactInfo] = []
        if task.artifacts:
            artifacts_info = [ArtifactInfo.from_artifact(a) for a in task.artifacts]

        text: str | None = None
        data: dict[str, Any] | None = None

        if artifacts_info:
            for ai in artifacts_info:
                if ai.text is not None:
                    text = ai.text
                    break
            for ai in artifacts_info:
                if ai.data is not None:
                    data = ai.data
                    break

        if text is None and task.status and task.status.message:
            text = _extract_text_from_parts(
                list(task.status.message.parts) if task.status.message.parts else []
            )

        state = task.status.state.value if task.status else "unknown"

        return cls(
            task_id=task.id,
            context_id=task.context_id,
            state=state,
            text=text,
            data=data,
            artifacts=artifacts_info,
            raw_task=task,
            raw_message=None,
        )

    @classmethod
    def from_message(cls, message: Message) -> ClientResult:
        """Build ClientResult from a direct-reply Message."""
        raw_parts = list(message.parts) if message.parts else []
        text = _extract_text_from_parts(raw_parts)
        data = _extract_data_from_parts(raw_parts)

        return cls(
            task_id=message.task_id or "",
            context_id=message.context_id,
            state="completed",
            text=text,
            data=data,
            artifacts=[],
            raw_task=None,
            raw_message=message,
        )


@dataclass(frozen=True)
class StreamEvent:
    """Single event yielded during streaming operations."""

    kind: str
    state: str | None
    text: str | None
    data: dict[str, Any] | None
    artifact_id: str | None
    is_final: bool

    raw: Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent

    task_id: str | None = None
    event_id: str | None = None

    @classmethod
    def from_raw(
        cls,
        event: Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent,
        *,
        event_id: str | None = None,
    ) -> StreamEvent:
        """Build StreamEvent from a raw protocol event."""
        if isinstance(event, Task):
            task_id = event.id
            state = event.status.state.value if event.status else None
            text: str | None = None
            data: dict[str, Any] | None = None
            if event.artifacts:
                for a in event.artifacts:
                    parts = list(a.parts) if a.parts else []
                    t = _extract_text_from_parts(parts)
                    if t is not None and text is None:
                        text = t
                    d = _extract_data_from_parts(parts)
                    if d is not None and data is None:
                        data = d
            is_final = state in ("completed", "failed", "canceled", "rejected")
            return cls(
                kind="task",
                state=state,
                text=text,
                data=data,
                artifact_id=None,
                is_final=is_final,
                raw=event,
                task_id=task_id,
                event_id=event_id,
            )

        if isinstance(event, TaskStatusUpdateEvent):
            state = event.status.state.value if event.status else None
            text = None
            if event.status and event.status.message:
                parts = list(event.status.message.parts) if event.status.message.parts else []
                text = _extract_text_from_parts(parts)
            return cls(
                kind="status",
                state=state,
                text=text,
                data=None,
                artifact_id=None,
                is_final=bool(event.final),
                raw=event,
                task_id=event.task_id,
                event_id=event_id,
            )

        # TaskArtifactUpdateEvent
        artifact = event.artifact
        parts = list(artifact.parts) if artifact.parts else []
        return cls(
            kind="artifact",
            state=None,
            text=_extract_text_from_parts(parts),
            data=_extract_data_from_parts(parts),
            artifact_id=artifact.artifact_id,
            is_final=bool(event.last_chunk),
            raw=event,
            task_id=event.task_id,
            event_id=event_id,
        )


@dataclass(frozen=True)
class ListResult:
    """Result from list_tasks() with pagination info."""

    tasks: list[ClientResult] = field(default_factory=list)
    next_page_token: str | None = None
    total_size: int | None = None
    page_size: int = 50
