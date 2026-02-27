"""Worker ABC, TaskContext, FileInfo, history wrappers, and TaskContextImpl."""

from __future__ import annotations

import base64
import logging
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from a2a.types import (
    Artifact,
    DataPart,
    FilePart,
    FileWithBytes,
    FileWithUri,
    Message,
    Part,
    Role,
    TaskArtifactUpdateEvent,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TextPart,
)

from agentserve.broker.base import CancelScope
from agentserve.event_emitter import EventEmitter
from agentserve.storage.base import Storage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileInfo:
    """Read-only wrapper for file parts from the user message."""

    content: bytes | None
    url: str | None
    filename: str | None
    media_type: str | None


@dataclass(frozen=True)
class HistoryMessage:
    """Read-only wrapper for a previous message within a task."""

    role: str
    text: str
    parts: list[Any]
    message_id: str


@dataclass(frozen=True)
class PreviousArtifact:
    """Read-only wrapper for an artifact from a previous turn."""

    artifact_id: str
    name: str | None
    parts: list[Any]


def _build_parts(
    *,
    text: str | None = None,
    data: dict | list | None = None,
    file_bytes: bytes | None = None,
    file_url: str | None = None,
    media_type: str | None = None,
    filename: str | None = None,
) -> list[Part]:
    """Build a list[Part] from simple Python types.

    Raises:
        ValueError: If no content parameter is provided.
    """
    parts: list[Part] = []

    if text is not None:
        parts.append(Part(TextPart(text=text)))

    if data is not None:
        parts.append(Part(DataPart(data=data)))

    if file_bytes is not None:
        encoded = base64.b64encode(file_bytes).decode("ascii")
        parts.append(
            Part(
                FilePart(
                    file=FileWithBytes(
                        bytes=encoded,
                        mime_type=media_type,
                        name=filename,
                    )
                )
            )
        )

    if file_url is not None:
        parts.append(
            Part(
                FilePart(
                    file=FileWithUri(
                        uri=file_url,
                        mime_type=media_type,
                        name=filename,
                    )
                )
            )
        )

    if not parts:
        raise ValueError(
            "At least one content parameter (text, data, file_bytes, file_url) "
            "must be provided."
        )

    return parts


def _extract_files(parts: list[Any]) -> list[FileInfo]:
    """Extract FileInfo wrappers from raw message parts."""
    files: list[FileInfo] = []
    for part in parts:
        root = getattr(part, "root", part)
        if not isinstance(root, FilePart):
            continue
        f = root.file
        content: bytes | None = None
        url: str | None = None
        if isinstance(f, FileWithBytes) and f.bytes:
            content = base64.b64decode(f.bytes)
        if isinstance(f, FileWithUri):
            url = f.uri
        files.append(
            FileInfo(
                content=content,
                url=url,
                filename=getattr(f, "name", None),
                media_type=getattr(f, "mime_type", None),
            )
        )
    return files


def _extract_data_parts(parts: list[Any]) -> list[dict]:
    """Extract structured data dicts from raw message parts."""
    result: list[dict] = []
    for part in parts:
        root = getattr(part, "root", part)
        if isinstance(root, DataPart) and isinstance(root.data, dict):
            result.append(root.data)
    return result


class TaskContext(ABC):
    """Execution context passed to ``Worker.handle()``.

    Attributes:
        task_id:    Current task identifier.
        context_id: Optional conversation / context identifier.
        message_id: Identifier of the triggering message.
        user_text:  The user's input as plain text.
        parts:      Raw message parts (text, files, etc.).
        metadata:   Arbitrary metadata forwarded from the request.
    """

    task_id: str
    context_id: str | None
    message_id: str
    user_text: str
    parts: list[Any]
    metadata: dict

    @property
    @abstractmethod
    def is_cancelled(self) -> bool:
        """Check whether cancellation has been requested for this task."""

    @property
    @abstractmethod
    def turn_ended(self) -> bool:
        """Whether the handler signaled the end of this processing turn.

        True after calling a terminal method (complete/fail/reject/respond)
        or requesting further input (request_input/request_auth).
        """

    @property
    @abstractmethod
    def files(self) -> list[FileInfo]:
        """All file parts from the user message as typed wrappers."""

    @property
    @abstractmethod
    def data_parts(self) -> list[dict]:
        """All structured data parts from the user message."""

    @property
    @abstractmethod
    def history(self) -> list[HistoryMessage]:
        """Previous messages within this task (excluding the current message)."""

    @property
    @abstractmethod
    def previous_artifacts(self) -> list[PreviousArtifact]:
        """Artifacts already produced by this task in previous turns."""

    @abstractmethod
    async def complete(self, text: str | None = None) -> None:
        """Mark the task as completed, optionally with a final text artifact."""

    @abstractmethod
    async def complete_json(self, data: dict | list) -> None:
        """Complete task with a JSON data artifact."""

    @abstractmethod
    async def fail(self, reason: str) -> None:
        """Mark the task as failed with an error reason."""

    @abstractmethod
    async def reject(self, reason: str | None = None) -> None:
        """Reject the task — agent decides not to perform it."""

    @abstractmethod
    async def request_input(self, question: str) -> None:
        """Transition to input-required state."""

    @abstractmethod
    async def request_auth(self, details: str | None = None) -> None:
        """Transition to auth-required state for secondary credentials."""

    @abstractmethod
    async def respond(self, text: str | None = None) -> None:
        """Complete the task with a direct message response (no artifact created)."""

    @abstractmethod
    async def send_status(self, message: str | None = None) -> None:
        """Emit an intermediate status update (state stays working)."""

    @abstractmethod
    async def emit_artifact(
        self,
        *,
        artifact_id: str,
        text: str | None = None,
        data: dict | list | None = None,
        file_bytes: bytes | None = None,
        file_url: str | None = None,
        media_type: str | None = None,
        filename: str | None = None,
        name: str | None = None,
        description: str | None = None,
        append: bool = False,
        last_chunk: bool = False,
        metadata: dict | None = None,
    ) -> None:
        """Emit an artifact update event and persist it."""

    @abstractmethod
    async def emit_text_artifact(
        self,
        text: str,
        *,
        artifact_id: str = "answer",
        append: bool = False,
        last_chunk: bool = False,
    ) -> None:
        """Emit a single-text artifact chunk."""

    @abstractmethod
    async def emit_data_artifact(
        self,
        data: dict | list,
        *,
        artifact_id: str = "answer",
        media_type: str = "application/json",
        append: bool = False,
        last_chunk: bool = False,
    ) -> None:
        """Emit a structured data artifact chunk."""

    @abstractmethod
    async def load_context(self) -> Any:
        """Load stored context for this task's context_id."""

    @abstractmethod
    async def update_context(self, context: Any) -> None:
        """Store context for this task's context_id."""


class TaskContextImpl(TaskContext):
    """Concrete implementation backed by an EventEmitter and Storage.

    Uses EventEmitter for task state changes and event broadcasting.
    Uses Storage directly for context load/update operations.
    """

    def __init__(
        self,
        *,
        task_id: str,
        context_id: str | None = None,
        message_id: str = "",
        user_text: str = "",
        parts: list[Any] | None = None,
        metadata: dict | None = None,
        emitter: EventEmitter,
        cancel_event: CancelScope,
        storage: Storage,
        history: list[HistoryMessage] | None = None,
        previous_artifacts: list[PreviousArtifact] | None = None,
    ) -> None:
        """Initialize the task context.

        Args:
            emitter: EventEmitter for state changes and event broadcasting.
            cancel_event: Scope for cooperative cancellation checks.
            storage: Storage for context load/update operations.
        """
        self.task_id = task_id
        self.context_id = context_id
        self.message_id = message_id
        self.user_text = user_text
        self.parts = parts if parts is not None else []
        self.metadata = metadata if metadata is not None else {}
        self._emitter = emitter
        self._cancel_event = cancel_event
        self._storage = storage
        self._history = history if history is not None else []
        self._previous_artifacts = (
            previous_artifacts if previous_artifacts is not None else []
        )
        self._turn_ended: bool = False
        self._responded_with_message: bool = False

    @property
    def is_cancelled(self) -> bool:
        """Check whether cancellation has been requested for this task."""
        return self._cancel_event.is_set()

    @property
    def turn_ended(self) -> bool:
        """Whether the handler signaled the end of this processing turn."""
        return self._turn_ended

    @property
    def history(self) -> list[HistoryMessage]:
        """Previous messages within this task (excluding the current message)."""
        return self._history

    @property
    def previous_artifacts(self) -> list[PreviousArtifact]:
        """Artifacts already produced by this task in previous turns."""
        return self._previous_artifacts

    @property
    def files(self) -> list[FileInfo]:
        """All file parts from the user message as typed wrappers."""
        return _extract_files(self.parts)

    @property
    def data_parts(self) -> list[dict]:
        """All structured data parts from the user message."""
        return _extract_data_parts(self.parts)

    async def complete(self, text: str | None = None) -> None:
        """Mark the task as completed, optionally with a final text artifact."""
        artifacts = None

        if text:
            final_parts = [Part(TextPart(text=text))]
            artifacts = [Artifact(artifact_id="final-answer", parts=final_parts)]

        await self._emitter.update_task(
            self.task_id,
            state=TaskState.completed,
            artifacts=artifacts,
        )

        if artifacts:
            await self._emitter.send_event(
                self.task_id,
                TaskArtifactUpdateEvent(
                    kind="artifact-update",
                    task_id=self.task_id,
                    context_id=self.context_id,
                    artifact=artifacts[0],
                    append=False,
                    last_chunk=True,
                ),
            )

        await self._emit_status(TaskState.completed)
        self._turn_ended = True

    async def complete_json(self, data: dict | list) -> None:
        """Complete task with a JSON data artifact."""
        data_parts = [Part(DataPart(data=data))]
        artifact = Artifact(
            artifact_id="final-answer",
            parts=data_parts,
            metadata={"media_type": "application/json"},
        )

        await self._emitter.update_task(
            self.task_id,
            state=TaskState.completed,
            artifacts=[artifact],
        )

        await self._emitter.send_event(
            self.task_id,
            TaskArtifactUpdateEvent(
                kind="artifact-update",
                task_id=self.task_id,
                context_id=self.context_id,
                artifact=artifact,
                append=False,
                last_chunk=True,
            ),
        )

        await self._emit_status(TaskState.completed)
        self._turn_ended = True

    async def fail(self, reason: str) -> None:
        """Mark the task as failed with an error reason."""
        await self._emitter.update_task(self.task_id, state=TaskState.failed)
        await self._emit_status(TaskState.failed, message_text=reason)
        self._turn_ended = True

    async def reject(self, reason: str | None = None) -> None:
        """Reject the task — agent decides not to perform it."""
        await self._emitter.update_task(self.task_id, state=TaskState.rejected)
        await self._emit_status(TaskState.rejected, message_text=reason)
        self._turn_ended = True

    async def request_input(self, question: str) -> None:
        """Transition to input-required state."""
        await self._emitter.update_task(self.task_id, state=TaskState.input_required)
        await self._emit_status(TaskState.input_required, message_text=question)
        self._turn_ended = True

    async def request_auth(self, details: str | None = None) -> None:
        """Transition to auth-required state for secondary credentials."""
        await self._emitter.update_task(self.task_id, state=TaskState.auth_required)
        await self._emit_status(TaskState.auth_required, message_text=details)
        self._turn_ended = True

    async def respond(self, text: str | None = None) -> None:
        """Complete the task with a direct message response (no artifact created)."""
        msg_parts = [Part(TextPart(text=text))] if text else []
        message = Message(
            role=Role.agent,
            parts=msg_parts,
            message_id=str(uuid.uuid4()),
            metadata=self.metadata,
        )
        await self._emitter.update_task(
            self.task_id,
            state=TaskState.completed,
            messages=[message],
        )
        await self._emitter.send_event(self.task_id, message)
        self._responded_with_message = True
        self._turn_ended = True

    async def send_status(self, message: str | None = None) -> None:
        """Emit an intermediate status update (state stays working)."""
        await self._emit_status(TaskState.working, message_text=message, final=False)

    async def emit_artifact(
        self,
        *,
        artifact_id: str,
        text: str | None = None,
        data: dict | list | None = None,
        file_bytes: bytes | None = None,
        file_url: str | None = None,
        media_type: str | None = None,
        filename: str | None = None,
        name: str | None = None,
        description: str | None = None,
        append: bool = False,
        last_chunk: bool = False,
        metadata: dict | None = None,
    ) -> None:
        """Emit an artifact update event and persist it."""
        parts = _build_parts(
            text=text,
            data=data,
            file_bytes=file_bytes,
            file_url=file_url,
            media_type=media_type,
            filename=filename,
        )
        artifact = Artifact(
            artifact_id=artifact_id,
            name=name,
            description=description,
            parts=parts,
            metadata=metadata or {},
        )
        await self._emitter.update_task(
            self.task_id,
            state=TaskState.working,
            artifacts=[artifact],
            append_artifact=append,
        )
        await self._emitter.send_event(
            self.task_id,
            TaskArtifactUpdateEvent(
                kind="artifact-update",
                task_id=self.task_id,
                context_id=self.context_id,
                artifact=artifact,
                append=append,
                last_chunk=last_chunk,
            ),
        )

    async def emit_text_artifact(
        self,
        text: str,
        *,
        artifact_id: str = "answer",
        append: bool = False,
        last_chunk: bool = False,
    ) -> None:
        """Emit a single-text artifact chunk."""
        await self.emit_artifact(
            artifact_id=artifact_id,
            text=text,
            append=append,
            last_chunk=last_chunk,
        )

    async def emit_data_artifact(
        self,
        data: dict | list,
        *,
        artifact_id: str = "answer",
        media_type: str = "application/json",
        append: bool = False,
        last_chunk: bool = False,
    ) -> None:
        """Emit a structured data artifact chunk."""
        await self.emit_artifact(
            artifact_id=artifact_id,
            data=data,
            metadata={"media_type": media_type},
            append=append,
            last_chunk=last_chunk,
        )

    async def _emit_status(
        self,
        state: TaskState,
        *,
        message_text: str | None = None,
        final: bool | None = None,
    ) -> None:
        """Build and broadcast a TaskStatusUpdateEvent."""
        if final is None:
            final = state in {
                TaskState.completed,
                TaskState.failed,
                TaskState.canceled,
                TaskState.rejected,
                TaskState.auth_required,
                TaskState.input_required,
            }
        status = TaskStatus(
            state=state,
            timestamp=datetime.now(UTC).isoformat(),
            message=(
                Message(
                    role=Role.agent,
                    parts=[Part(TextPart(text=message_text))],
                    message_id=str(uuid.uuid4()),
                    metadata=self.metadata,
                )
                if message_text
                else None
            ),
        )
        await self._emitter.send_event(
            self.task_id,
            TaskStatusUpdateEvent(
                kind="status-update",
                task_id=self.task_id,
                context_id=self.context_id,
                status=status,
                final=final,
            ),
        )

    async def load_context(self) -> Any:
        """Load stored context for this task's context_id."""
        if not self.context_id:
            return None
        return await self._storage.load_context(self.context_id)

    async def update_context(self, context: Any) -> None:
        """Store context for this task's context_id."""
        if not self.context_id:
            return
        await self._storage.update_context(self.context_id, context)


class Worker(ABC):
    """Abstract base class — implement handle() to build an A2A agent."""

    @abstractmethod
    async def handle(self, ctx: TaskContext) -> None:
        """Process a task using ctx methods to emit results and control lifecycle."""
