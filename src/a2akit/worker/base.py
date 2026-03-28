"""Worker ABC, TaskContext, FileInfo, history wrappers, and TaskContextImpl."""

from __future__ import annotations

import base64
import logging
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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

from a2akit.schema import DIRECT_REPLY_KEY, DirectReply
from a2akit.storage.base import (
    TERMINAL_STATES,
    ArtifactWrite,
    ConcurrencyError,
    Storage,
    TaskTerminalStateError,
)

if TYPE_CHECKING:
    from a2akit.broker.base import CancelScope
    from a2akit.dependencies import DependencyContainer
    from a2akit.event_emitter import EventEmitter

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
    data: dict[str, Any] | list[Any] | None = None,
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
            "At least one content parameter (text, data, file_bytes, file_url) must be provided."
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


def _extract_data_parts(parts: list[Any]) -> list[dict[str, Any]]:
    """Extract structured data dicts from raw message parts."""
    result: list[dict[str, Any]] = []
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
    metadata: dict[str, Any]

    @property
    @abstractmethod
    def request_context(self) -> dict[str, Any]:
        """Transient request data from middleware.

        Contains secrets, headers, and other per-request data that
        was NOT persisted to Storage. Populated by middleware in
        ``before_dispatch``. Empty dict if no middleware is registered.

        This is separate from ``self.metadata`` which contains
        persisted data from ``message.metadata``.
        """

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
    def data_parts(self) -> list[dict[str, Any]]:
        """All structured data parts from the user message."""

    @property
    @abstractmethod
    def history(self) -> list[HistoryMessage]:
        """Previous messages within this task (excluding the current message)."""

    @property
    @abstractmethod
    def previous_artifacts(self) -> list[PreviousArtifact]:
        """Artifacts already produced by this task in previous turns."""

    @property
    @abstractmethod
    def reference_task_ids(self) -> list[str]:
        """Task IDs referenced by the triggering message for cross-task context."""

    @property
    @abstractmethod
    def message_extensions(self) -> list[str]:
        """Extension URIs declared on the triggering message."""

    @property
    @abstractmethod
    def deps(self) -> DependencyContainer:
        """Dependency container registered on the server.

        Access by type key or string key::

            db = ctx.deps[DatabasePool]
            key = ctx.deps.get("api_key", "default")
        """

    @abstractmethod
    def accepts(self, mime_type: str) -> bool:
        """Check whether the client accepts the given output MIME type.

        Returns ``True`` if the client listed this type in
        ``acceptedOutputModes``, or if no filter was specified
        (absent or empty means the client accepts everything).

        Common MIME types::

            ctx.accepts("text/plain")
            ctx.accepts("application/json")
            ctx.accepts("text/html")
            ctx.accepts("text/csv")
            ctx.accepts("text/markdown")
            ctx.accepts("application/pdf")
            ctx.accepts("image/png")
            ctx.accepts("image/jpeg")
            ctx.accepts("audio/mpeg")
            ctx.accepts("video/mp4")
            ctx.accepts("application/xml")
            ctx.accepts("application/octet-stream")

        Example::

            async def handle(self, ctx: TaskContext) -> None:
                if ctx.accepts("application/json"):
                    await ctx.complete_json({"revenue": 42000})
                else:
                    await ctx.complete("Revenue: 42,000 €")
        """

    @abstractmethod
    async def complete(
        self, text: str | None = None, *, artifact_id: str = "final-answer"
    ) -> None:
        """Mark the task as completed, optionally with a final text artifact."""

    @abstractmethod
    async def complete_json(
        self, data: dict[str, Any] | list[Any], *, artifact_id: str = "final-answer"
    ) -> None:
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
    async def request_auth(
        self,
        details: str | None = None,
        *,
        schemes: list[str] | None = None,
        credentials_hint: str | None = None,
        auth_url: str | None = None,
    ) -> None:
        """Transition to auth-required state for secondary credentials.

        Args:
            details: Human-readable text explanation.
            schemes: Required auth schemes (e.g. ``["Bearer", "OAuth2"]``).
            credentials_hint: Hint about what credentials are needed.
            auth_url: URL where the client can obtain credentials.
        """

    @abstractmethod
    async def respond(self, text: str | None = None) -> None:
        """Complete the task with a direct message response (no artifact created)."""

    @abstractmethod
    async def reply_directly(self, text: str) -> None:
        """Return a Message directly without task tracking.

        The HTTP response to ``message:send`` will be
        ``{"message": {...}}`` instead of ``{"task": {...}}``.
        The task is still created internally for lifecycle management
        but the client receives only the message.

        **Streaming note:** When the request arrives via ``message:stream``,
        this method uses the Task lifecycle stream pattern (Task snapshot
        followed by events and a terminal status update) rather than a
        message-only stream.  This is spec-conformant (§3.1.2) — servers
        may always respond with a Task lifecycle stream.
        """

    @abstractmethod
    async def send_status(self, message: str | None = None) -> None:
        """Emit an intermediate status update (state stays working).

        When ``message`` is provided, the status message is persisted
        in Storage so that polling clients (``tasks/get``) can see
        progress — not just SSE subscribers.  The message is stored
        in ``task.status.message`` only (NOT appended to history)
        to keep the write lightweight.

        When ``message`` is ``None``, only a bare working-state event
        is broadcast (no storage write).
        """

    @abstractmethod
    async def emit_artifact(
        self,
        *,
        artifact_id: str,
        text: str | None = None,
        data: dict[str, Any] | list[Any] | None = None,
        file_bytes: bytes | None = None,
        file_url: str | None = None,
        media_type: str | None = None,
        filename: str | None = None,
        name: str | None = None,
        description: str | None = None,
        append: bool = False,
        last_chunk: bool = False,
        metadata: dict[str, Any] | None = None,
        extensions: list[str] | None = None,
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
        data: dict[str, Any] | list[Any],
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
        metadata: dict[str, Any] | None = None,
        emitter: EventEmitter,
        cancel_event: CancelScope,
        storage: Storage,
        history: list[HistoryMessage] | None = None,
        previous_artifacts: list[PreviousArtifact] | None = None,
        initial_version: int | None = None,
        request_context: dict[str, Any] | None = None,
        deps: DependencyContainer | None = None,
        accepted_output_modes: list[str] | None = None,
        deferred_storage: bool = False,
    ) -> None:
        """Initialize the task context.

        Args:
            emitter: EventEmitter for state changes and event broadcasting.
            cancel_event: Scope for cooperative cancellation checks.
            storage: Storage for context load/update operations.
            initial_version: Optimistic concurrency version from storage.
                DB backends use this for ``UPDATE ... WHERE version = ?``.
                InMemory ignores this parameter.
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
        self._previous_artifacts = previous_artifacts if previous_artifacts is not None else []
        self._turn_ended: bool = False
        self._version: int | None = initial_version
        self._request_context = request_context or {}
        from a2akit.dependencies import DependencyContainer

        self._deps = deps if deps is not None else DependencyContainer()
        self._accepted_output_modes = accepted_output_modes
        self._reference_task_ids: list[str] = []
        self._message_extensions: list[str] = []
        # Write-batching: buffer artifact writes, flush periodically or on terminal
        self._pending_artifacts: list[ArtifactWrite] = []
        self._last_flush: float = time.monotonic()
        self._flush_interval: float = 0.5  # seconds
        self._flush_count: int = 10  # max buffered chunks before flush
        self._deferred_storage: bool = deferred_storage

    def _make_agent_message(
        self, parts: list[Part], *, metadata: dict[str, Any] | None = None
    ) -> Message:
        """Create an agent Message pre-filled with task_id and context_id."""
        return Message(
            role=Role.agent,
            parts=parts,
            message_id=str(uuid.uuid4()),
            task_id=self.task_id,
            context_id=self.context_id,
            metadata=metadata if metadata is not None else self.metadata,
        )

    async def _versioned_update(self, task_id: str, **kwargs: Any) -> None:
        """Call emitter.update_task with optimistic concurrency version.

        Passes ``expected_version`` and updates ``self._version`` from
        the returned new version so subsequent writes stay in sync.

        On ``ConcurrencyError`` (version mismatch from a concurrent
        writer), re-loads the task:
        - If terminal → raises ``TaskTerminalStateError`` so the
          caller stops processing (e.g. force-cancel won the race).
        - If non-terminal → retries once with the freshly loaded version.
        """
        try:
            new_version = await self._emitter.update_task(
                task_id, expected_version=self._version, **kwargs
            )
            self._version = new_version
        except ConcurrencyError as exc:
            task = await self._storage.load_task(task_id)
            if task is None or task.status.state in TERMINAL_STATES:
                raise TaskTerminalStateError(
                    f"Task {task_id} reached terminal state during update"
                ) from exc
            # Non-terminal version mismatch: retry with fresh version.
            fresh_version = exc.current_version or await self._storage.get_version(task_id)
            new_version = await self._emitter.update_task(
                task_id, expected_version=fresh_version, **kwargs
            )
            self._version = new_version

    async def _maybe_flush(self) -> None:
        """Flush pending artifacts to DB if interval or count threshold exceeded."""
        if self._deferred_storage or not self._pending_artifacts:
            return
        now = time.monotonic()
        if (
            now - self._last_flush >= self._flush_interval
            or len(self._pending_artifacts) >= self._flush_count
        ):
            await self._flush_artifacts()

    async def _flush_artifacts(self) -> None:
        """Write all buffered artifacts to storage in one call."""
        if not self._pending_artifacts:
            return
        batch = self._pending_artifacts
        self._pending_artifacts = []
        await self._versioned_update(self.task_id, artifacts=batch)
        self._last_flush = time.monotonic()

    @property
    def request_context(self) -> dict[str, Any]:
        """Transient request data from middleware."""
        return self._request_context

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
    def data_parts(self) -> list[dict[str, Any]]:
        """All structured data parts from the user message."""
        return _extract_data_parts(self.parts)

    @property
    def reference_task_ids(self) -> list[str]:
        """Task IDs referenced by the triggering message."""
        return self._reference_task_ids

    @property
    def message_extensions(self) -> list[str]:
        """Extension URIs declared on the triggering message."""
        return self._message_extensions

    @property
    def deps(self) -> DependencyContainer:
        """Dependency container registered on the server."""
        return self._deps

    def accepts(self, mime_type: str) -> bool:
        """Check whether the client accepts the given output MIME type."""
        if not self._accepted_output_modes:
            return True
        return mime_type in self._accepted_output_modes

    async def complete(
        self, text: str | None = None, *, artifact_id: str = "final-answer"
    ) -> None:
        """Mark the task as completed, optionally with a final text artifact."""
        artifact: Artifact | None = None

        if text is not None:
            final_parts = [Part(TextPart(text=text))]
            artifact = Artifact(artifact_id=artifact_id, parts=final_parts)

        # Drain pending chunks + optional final artifact = 1 atomic DB write
        pending = self._pending_artifacts
        self._pending_artifacts = []
        all_artifacts = pending + ([ArtifactWrite(artifact)] if artifact else [])

        await self._versioned_update(
            self.task_id,
            state=TaskState.completed,
            artifacts=all_artifacts or None,
        )

        if artifact is not None:
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

    async def complete_json(
        self, data: dict[str, Any] | list[Any], *, artifact_id: str = "final-answer"
    ) -> None:
        """Complete task with a JSON data artifact."""
        data_parts = [Part(DataPart(data=data))]
        artifact = Artifact(
            artifact_id=artifact_id,
            parts=data_parts,
            metadata={"media_type": "application/json"},
        )

        # Drain pending chunks + JSON artifact = 1 atomic DB write
        pending = self._pending_artifacts
        self._pending_artifacts = []
        all_artifacts = [*pending, ArtifactWrite(artifact)]

        await self._versioned_update(
            self.task_id,
            state=TaskState.completed,
            artifacts=all_artifacts,
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
        pending = self._pending_artifacts
        self._pending_artifacts = []

        fail_message = self._make_agent_message([Part(TextPart(text=reason))])
        await self._versioned_update(
            self.task_id,
            state=TaskState.failed,
            status_message=fail_message,
            messages=[fail_message],
            artifacts=pending or None,
        )
        await self._emit_status(TaskState.failed, message=fail_message)
        self._turn_ended = True

    async def reject(self, reason: str | None = None) -> None:
        """Reject the task — agent decides not to perform it."""
        pending = self._pending_artifacts
        self._pending_artifacts = []

        reject_text = reason or "Task rejected."
        reject_message = self._make_agent_message([Part(TextPart(text=reject_text))])
        await self._versioned_update(
            self.task_id,
            state=TaskState.rejected,
            status_message=reject_message,
            messages=[reject_message],
            artifacts=pending or None,
        )
        await self._emit_status(TaskState.rejected, message=reject_message)
        self._turn_ended = True

    async def request_input(self, question: str) -> None:
        """Transition to input-required state."""
        pending = self._pending_artifacts
        self._pending_artifacts = []

        input_message = self._make_agent_message([Part(TextPart(text=question))])
        await self._versioned_update(
            self.task_id,
            state=TaskState.input_required,
            status_message=input_message,
            messages=[input_message],
            artifacts=pending or None,
        )
        await self._emit_status(TaskState.input_required, message=input_message)
        self._turn_ended = True

    async def request_auth(
        self,
        details: str | None = None,
        *,
        schemes: list[str] | None = None,
        credentials_hint: str | None = None,
        auth_url: str | None = None,
    ) -> None:
        """Transition to auth-required state for secondary credentials."""
        parts: list[Part] = []

        if details:
            parts.append(Part(TextPart(text=details)))

        if schemes or credentials_hint or auth_url:
            auth_data: dict[str, Any] = {}
            if schemes:
                auth_data["schemes"] = schemes
            if credentials_hint:
                auth_data["credentialsHint"] = credentials_hint
            if auth_url:
                auth_data["authUrl"] = auth_url
            parts.append(Part(DataPart(data=auth_data)))

        if not parts:
            parts.append(Part(TextPart(text="Authentication required.")))

        pending = self._pending_artifacts
        self._pending_artifacts = []

        auth_message = self._make_agent_message(parts)
        await self._versioned_update(
            self.task_id,
            state=TaskState.auth_required,
            status_message=auth_message,
            messages=[auth_message],
            artifacts=pending or None,
        )
        await self._emit_status(TaskState.auth_required, message=auth_message)
        self._turn_ended = True

    async def respond(self, text: str | None = None) -> None:
        """Complete the task with a status message (no artifact created)."""
        pending = self._pending_artifacts
        self._pending_artifacts = []

        msg_parts = [Part(TextPart(text=text))] if text else []
        respond_message = self._make_agent_message(msg_parts)
        await self._versioned_update(
            self.task_id,
            state=TaskState.completed,
            status_message=respond_message,
            messages=[respond_message],
            artifacts=pending or None,
        )
        await self._emit_status(TaskState.completed, message=respond_message)
        self._turn_ended = True

    async def reply_directly(self, text: str) -> None:
        """Return a Message directly without task tracking.

        The task is marked as completed internally but the HTTP response
        to ``message:send`` will be ``{"message": {...}}`` instead of
        ``{"task": {...}}``.

        The direct-reply marker is stored in ``Task.metadata`` (not
        ``Message.metadata``) so it survives storage round-trips
        without polluting user-facing message data.  The EventBus
        receives a :class:`DirectReply` wrapper so subscribers can
        distinguish this from a normal agent message.
        """
        pending = self._pending_artifacts
        self._pending_artifacts = []

        message = self._make_agent_message([Part(TextPart(text=text))])
        await self._versioned_update(
            self.task_id,
            state=TaskState.completed,
            status_message=message,
            messages=[message],
            task_metadata={DIRECT_REPLY_KEY: message.message_id},
            artifacts=pending or None,
        )
        await self._emitter.send_event(self.task_id, DirectReply(message=message))
        await self._emit_status(TaskState.completed, message=message)
        self._turn_ended = True

    async def send_status(self, message: str | None = None) -> None:
        """Emit an intermediate status update (state stays working)."""
        status_msg = None
        if message is not None:
            status_msg = self._make_agent_message([Part(TextPart(text=message))])

        # SSE first — streaming clients see status immediately
        await self._emit_status(TaskState.working, message=status_msg, final=False)

        # Flush pending artifacts together with the status write
        if not self._deferred_storage and status_msg is not None:
            pending = self._pending_artifacts
            self._pending_artifacts = []
            await self._versioned_update(
                self.task_id,
                state=TaskState.working,
                status_message=status_msg,
                artifacts=pending or None,
            )
            self._last_flush = time.monotonic()

    async def emit_artifact(
        self,
        *,
        artifact_id: str,
        text: str | None = None,
        data: dict[str, Any] | list[Any] | None = None,
        file_bytes: bytes | None = None,
        file_url: str | None = None,
        media_type: str | None = None,
        filename: str | None = None,
        name: str | None = None,
        description: str | None = None,
        append: bool = False,
        last_chunk: bool = False,
        metadata: dict[str, Any] | None = None,
        extensions: list[str] | None = None,
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
            extensions=extensions,
        )

        # SSE first — streaming clients see every chunk immediately
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

        # Buffer for DB — polling clients see periodic snapshots
        self._pending_artifacts.append(ArtifactWrite(artifact, append=append))
        await self._maybe_flush()

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
        data: dict[str, Any] | list[Any],
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
        message: Message | None = None,
        message_text: str | None = None,
        final: bool | None = None,
    ) -> None:
        """Build and broadcast a TaskStatusUpdateEvent.

        ``final`` controls whether the SSE stream ends after this event.
        It does NOT mean the task has reached a terminal state.  States
        like ``auth_required`` and ``input_required`` set ``final=True``
        because the client must reconnect after these responses, even
        though the task itself is not terminal.

        When ``message`` is provided, it is used directly (reusing the
        persisted message for consistency between SSE and polling).
        When only ``message_text`` is provided, a new ephemeral message
        is created (appropriate for intermediate status updates).
        """
        if final is None:
            final = state in {
                TaskState.completed,
                TaskState.failed,
                TaskState.canceled,
                TaskState.rejected,
                TaskState.auth_required,
                TaskState.input_required,
            }

        status_message = message
        if status_message is None and message_text:
            status_message = Message(
                role=Role.agent,
                parts=[Part(TextPart(text=message_text))],
                message_id=str(uuid.uuid4()),
                task_id=self.task_id,
                context_id=self.context_id,
                metadata=self.metadata,
            )

        status = TaskStatus(
            state=state,
            timestamp=datetime.now(UTC).isoformat(),
            message=status_message,
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

    # Context operations bypass EventEmitter intentionally:
    # - Context is per-user state, not task state
    # - No event broadcasting needed for context changes
    # - EventEmitter should not be extended for non-task concerns

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
