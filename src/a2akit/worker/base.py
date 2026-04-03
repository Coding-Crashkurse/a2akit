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

import anyio
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
        """Check whether cancellation has been requested for this task.

        Poll this in long-running loops to support cooperative cancellation.

        Example::

            for chunk in large_dataset:
                if ctx.is_cancelled:
                    await ctx.fail("Cancelled by user.")
                    return
                await ctx.emit_text_artifact(process(chunk), append=True)
        """

    @property
    @abstractmethod
    def turn_ended(self) -> bool:
        """Whether the handler already signaled the end of this turn.

        ``True`` after calling any terminal method (``complete``, ``fail``,
        ``reject``, ``respond``, ``reply_directly``) or a method that
        pauses for client input (``request_input``, ``request_auth``).
        """

    @property
    @abstractmethod
    def files(self) -> list[FileInfo]:
        """File attachments from the user message.

        Each ``FileInfo`` provides ``content`` (decoded bytes),
        ``url``, ``filename``, and ``media_type``.

        Example::

            for f in ctx.files:
                if f.media_type == "image/png" and f.content:
                    result = analyze_image(f.content)
                    await ctx.complete(result)
                    return
        """

    @property
    @abstractmethod
    def data_parts(self) -> list[dict[str, Any]]:
        """Structured data parts from the user message.

        Returns each ``DataPart`` as a plain dict. Use this when
        clients send JSON payloads alongside text.

        Example::

            for part in ctx.data_parts:
                if "query" in part:
                    results = run_query(part["query"])
                    await ctx.complete_json(results)
                    return
        """

    @property
    @abstractmethod
    def history(self) -> list[HistoryMessage]:
        """Previous messages in this task (excluding the current message).

        Each ``HistoryMessage`` has ``role``, ``text``, ``parts``, and
        ``message_id``. Empty on the first turn. Use this for multi-turn
        conversation context.

        Example::

            if ctx.history:
                # Continuing a conversation
                prev = ctx.history[-1]
                await ctx.complete(f"You previously said: {prev.text}")
            else:
                await ctx.request_input("What would you like to discuss?")
        """

    @property
    @abstractmethod
    def previous_artifacts(self) -> list[PreviousArtifact]:
        """Artifacts produced by this task in previous turns.

        Each ``PreviousArtifact`` has ``artifact_id``, ``name``, and
        ``parts``. Use this to build on earlier results in multi-turn
        workflows (e.g. iterative refinement).

        Example::

            if ctx.previous_artifacts:
                prev = ctx.previous_artifacts[-1]
                await ctx.complete(f"Refining: {prev.parts}")
        """

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
        """Complete the task, optionally producing a text artifact as result.

        This is the primary way to finish a task. When ``text`` is provided,
        it is persisted as an ``Artifact`` that clients can retrieve later.
        When called without arguments, the task is marked completed with no
        output — useful after streaming artifacts via ``emit_*`` methods.

        Use ``complete()`` when the response is a **deliverable** (a document,
        an answer, generated code). Use ``respond()`` instead when the reply
        is conversational and does not need to be stored as an artifact.

        Example::

            # Simple request-response
            await ctx.complete(f"The answer is {result}")

            # Complete without text after streaming chunks
            for chunk in chunks:
                await ctx.emit_text_artifact(chunk, append=True)
            await ctx.complete()

            # Custom artifact ID for multi-artifact tasks
            await ctx.complete("Final report", artifact_id="report")
        """

    @abstractmethod
    async def complete_json(
        self, data: dict[str, Any] | list[Any], *, artifact_id: str = "final-answer"
    ) -> None:
        """Complete the task with a structured JSON artifact.

        Creates an artifact with ``media_type: application/json`` in its
        metadata. Use this when the client expects machine-readable data
        rather than human-readable text.

        Example::

            await ctx.complete_json({"revenue": 42000, "currency": "EUR"})

            # Content-negotiation pattern
            if ctx.accepts("application/json"):
                await ctx.complete_json({"status": "ok", "count": 5})
            else:
                await ctx.complete("Status: ok (5 items)")
        """

    @abstractmethod
    async def fail(self, reason: str) -> None:
        """Mark the task as failed with an error message.

        The reason is sent to the client as a status message. Use this
        for errors that the agent encountered while processing (API
        failures, invalid data, timeouts, etc.).

        Example::

            try:
                result = await call_external_api()
            except APIError as e:
                await ctx.fail(f"API call failed: {e}")
                return
            await ctx.complete(result)
        """

    @abstractmethod
    async def reject(self, reason: str | None = None) -> None:
        """Reject the task — the agent decides not to perform it.

        Unlike ``fail()`` which indicates an error during processing,
        ``reject()`` means the agent **chose** not to handle the request
        (e.g. out of scope, policy violation, unsupported language).

        Example::

            if "delete" in ctx.user_text.lower():
                await ctx.reject("I cannot perform destructive actions.")
                return
        """

    @abstractmethod
    async def request_input(self, question: str) -> None:
        """Ask the user for additional input (multi-turn conversation).

        Transitions the task to ``input-required`` state. The client
        should display the question and send a follow-up message with
        the same ``context_id`` to continue the conversation.

        Example::

            if not ctx.history:
                await ctx.request_input("What language should I translate to?")
                return
            # Second turn — history contains the original request
            target_lang = ctx.user_text
            await ctx.complete(translate(ctx.history[0].text, target_lang))
        """

    @abstractmethod
    async def request_auth(
        self,
        details: str | None = None,
        *,
        schemes: list[str] | None = None,
        credentials_hint: str | None = None,
        auth_url: str | None = None,
    ) -> None:
        """Ask the client for additional credentials.

        Transitions the task to ``auth-required`` state. Use this when
        the worker needs credentials beyond what the initial request
        provided (e.g. an OAuth token for a third-party service).

        Args:
            details: Human-readable explanation shown to the user.
            schemes: Required auth schemes (e.g. ``["Bearer", "OAuth2"]``).
            credentials_hint: Hint about what credentials are needed.
            auth_url: URL where the client can obtain credentials.

        Example::

            if "github_token" not in ctx.request_context:
                await ctx.request_auth(
                    "I need a GitHub token to access your repositories.",
                    schemes=["Bearer"],
                    auth_url="https://github.com/settings/tokens",
                )
                return
        """

    @abstractmethod
    async def respond(self, text: str | None = None) -> None:
        """Complete the task with a status message, without creating an artifact.

        Use this instead of ``complete()`` when the reply is conversational
        or ephemeral — an acknowledgement, a clarification, a "done" — and
        does not need to be persisted as a retrievable artifact.

        Example::

            # Acknowledge a side-effect action
            await send_email(to=recipient, body=draft)
            await ctx.respond("Email sent successfully.")

            # Compare with complete() which creates a persistent artifact:
            # await ctx.complete("Here is the generated report...")
        """

    @abstractmethod
    async def reply_directly(self, text: str) -> None:
        """Return a ``Message`` response instead of a ``Task`` response.

        The HTTP response to ``message:send`` will be
        ``{"message": {...}}`` instead of ``{"task": {...}}``.
        Use this for lightweight, stateless interactions where the
        client does not need to track a task (e.g. health checks,
        simple Q&A, quick lookups).

        The task is still created internally for lifecycle management,
        but the client receives only the message.

        Example::

            # Fast, stateless reply — client gets a Message, not a Task
            await ctx.reply_directly(f"Current time: {now()}")

        **Streaming note:** When the request arrives via ``message:stream``,
        this method falls back to the standard Task lifecycle stream
        (Task snapshot + events + terminal status). This is
        spec-conformant (A2A §3.1.2).
        """

    @abstractmethod
    async def send_status(self, message: str | None = None) -> None:
        """Emit an intermediate progress update while the task keeps working.

        Use this to inform the client about long-running operations.
        The task stays in ``working`` state. SSE subscribers see the
        update immediately. When ``message`` is provided, the status
        is also persisted so that polling clients (``tasks/get``) can
        see progress.

        When ``message`` is ``None``, only a bare working-state event
        is broadcast (no storage write).

        Example::

            await ctx.send_status("Downloading dataset...")
            data = await download(url)
            await ctx.send_status("Analyzing 10,000 rows...")
            result = analyze(data)
            await ctx.complete(result)
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
        """Emit an artifact with full control over content and metadata.

        This is the low-level artifact method. For common cases, prefer
        the convenience wrappers:

        - ``emit_text_artifact()`` — single text chunk
        - ``emit_data_artifact()`` — structured JSON data

        Artifacts are streamed to SSE subscribers immediately and
        batched for storage persistence. Call ``complete()`` after
        emitting all artifacts to finalize the task.

        Provide exactly one content argument: ``text``, ``data``,
        ``file_bytes``, or ``file_url``.

        Example::

            # Stream a file artifact
            await ctx.emit_artifact(
                artifact_id="export",
                file_bytes=csv_bytes,
                media_type="text/csv",
                filename="report.csv",
                name="Monthly Report",
                last_chunk=True,
            )
            await ctx.complete()

            # Multi-part streaming with append
            for i, chunk in enumerate(chunks):
                await ctx.emit_artifact(
                    artifact_id="story",
                    text=chunk,
                    append=i > 0,
                    last_chunk=i == len(chunks) - 1,
                )
            await ctx.complete()
        """

    async def emit_text_artifact(
        self,
        text: str,
        *,
        artifact_id: str = "answer",
        append: bool = False,
        last_chunk: bool = False,
    ) -> None:
        """Emit a text artifact chunk — the most common streaming pattern.

        Convenience wrapper around ``emit_artifact()``. Use ``append=True``
        for all chunks after the first. Set ``last_chunk=True`` on the
        final chunk so clients know the artifact is complete.

        Example::

            # Stream token by token
            for i, token in enumerate(llm_stream):
                await ctx.emit_text_artifact(
                    token,
                    append=i > 0,
                    last_chunk=i == total - 1,
                )
            await ctx.complete()

            # Single-shot artifact (no streaming)
            await ctx.emit_text_artifact("Here is the summary.", last_chunk=True)
            await ctx.complete()
        """
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
        """Emit a structured data artifact chunk.

        Convenience wrapper around ``emit_artifact()`` for JSON-serializable
        data. Sets ``media_type`` in artifact metadata (defaults to
        ``application/json``).

        Example::

            # Emit a structured result
            await ctx.emit_data_artifact(
                {"users": users, "total": len(users)},
                last_chunk=True,
            )
            await ctx.complete()

            # Stream rows incrementally
            for i, row in enumerate(query_results):
                await ctx.emit_data_artifact(
                    row,
                    append=i > 0,
                    last_chunk=i == total - 1,
                )
            await ctx.complete()
        """
        await self.emit_artifact(
            artifact_id=artifact_id,
            data=data,
            metadata={"media_type": media_type},
            append=append,
            last_chunk=last_chunk,
        )

    @abstractmethod
    async def load_context(self) -> Any:
        """Load persisted conversation context for this ``context_id``.

        Returns whatever was previously saved via ``update_context()``,
        or ``None`` if nothing was stored or ``context_id`` is not set.
        Use this to maintain state across turns in multi-turn conversations.

        Example::

            prefs = await ctx.load_context() or {}
            lang = prefs.get("language", "en")
            await ctx.complete(translate(ctx.user_text, lang))
        """

    @abstractmethod
    async def update_context(self, context: Any) -> None:
        """Persist conversation context for this ``context_id``.

        The context is stored per ``context_id`` and available in
        subsequent turns via ``load_context()``. Pass any
        JSON-serializable value. Does nothing if ``context_id`` is not set.

        Example::

            # Remember user preferences across turns
            prefs = await ctx.load_context() or {}
            prefs["language"] = ctx.user_text
            await ctx.update_context(prefs)
            await ctx.respond("Language preference saved.")
        """


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
            metadata=metadata if metadata is not None else {},
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
        try:
            await self._versioned_update(self.task_id, artifacts=batch)
        except ConcurrencyError:
            # Intermediate flush lost to concurrent modification — re-buffer.
            # Artifacts will be written with the next flush or terminal transition.
            self._pending_artifacts = batch + self._pending_artifacts
            logger.warning("Artifact flush skipped (concurrent modification) for %s", self.task_id)
            return  # Don't update _last_flush — retry sooner
        except Exception:
            self._pending_artifacts = batch + self._pending_artifacts
            raise
        self._last_flush = time.monotonic()

    async def _terminal_transition(
        self,
        state: TaskState,
        *,
        message: Message | None = None,
        artifacts: list[ArtifactWrite] | None = None,
        task_metadata: dict[str, Any] | None = None,
        pre_events: list[Any] | None = None,
    ) -> None:
        """Unified terminal transition: drain pending → persist → emit → end turn."""
        if self._turn_ended:
            raise RuntimeError(
                f"Cannot transition to {state.value}: turn already ended. "
                "Only one lifecycle method (complete/fail/reject/respond/"
                "request_input/request_auth/reply_directly) may be called per turn."
            )
        pending = self._pending_artifacts
        self._pending_artifacts = []
        all_artifacts = [*pending, *(artifacts or [])]

        update_kwargs: dict[str, Any] = {"state": state}
        if all_artifacts:
            update_kwargs["artifacts"] = all_artifacts
        if message is not None:
            update_kwargs["status_message"] = message
            update_kwargs["messages"] = [message]
        if task_metadata is not None:
            update_kwargs["task_metadata"] = task_metadata

        try:
            await self._versioned_update(self.task_id, **update_kwargs)
        except Exception:
            self._pending_artifacts = pending + self._pending_artifacts
            raise

        # Shield post-write SSE emission from cancellation — the DB write
        # succeeded, so the final event MUST reach subscribers.
        with anyio.CancelScope(shield=True):
            for evt in pre_events or []:
                await self._emitter.send_event(self.task_id, evt)

            await self._emit_status(state, message=message)
            self._turn_ended = True

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
        artifacts: list[ArtifactWrite] | None = None
        pre_events: list[Any] | None = None

        if text is not None:
            artifact = Artifact(artifact_id=artifact_id, parts=[Part(TextPart(text=text))])
            artifacts = [ArtifactWrite(artifact)]
            pre_events = [
                TaskArtifactUpdateEvent(
                    kind="artifact-update",
                    task_id=self.task_id,
                    context_id=self.context_id,
                    artifact=artifact,
                    append=False,
                    last_chunk=True,
                )
            ]

        await self._terminal_transition(
            TaskState.completed, artifacts=artifacts, pre_events=pre_events
        )

    async def complete_json(
        self, data: dict[str, Any] | list[Any], *, artifact_id: str = "final-answer"
    ) -> None:
        """Complete task with a JSON data artifact."""
        artifact = Artifact(
            artifact_id=artifact_id,
            parts=[Part(DataPart(data=data))],
            metadata={"media_type": "application/json"},
        )
        await self._terminal_transition(
            TaskState.completed,
            artifacts=[ArtifactWrite(artifact)],
            pre_events=[
                TaskArtifactUpdateEvent(
                    kind="artifact-update",
                    task_id=self.task_id,
                    context_id=self.context_id,
                    artifact=artifact,
                    append=False,
                    last_chunk=True,
                )
            ],
        )

    async def fail(self, reason: str) -> None:
        """Mark the task as failed with an error reason."""
        msg = self._make_agent_message([Part(TextPart(text=reason))])
        await self._terminal_transition(TaskState.failed, message=msg)

    async def reject(self, reason: str | None = None) -> None:
        """Reject the task — agent decides not to perform it."""
        msg = self._make_agent_message([Part(TextPart(text=reason or "Task rejected."))])
        await self._terminal_transition(TaskState.rejected, message=msg)

    async def request_input(self, question: str) -> None:
        """Transition to input-required state."""
        msg = self._make_agent_message([Part(TextPart(text=question))])
        await self._terminal_transition(TaskState.input_required, message=msg)

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
        if details is not None:
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
        msg = self._make_agent_message(parts)
        await self._terminal_transition(TaskState.auth_required, message=msg)

    async def respond(self, text: str | None = None) -> None:
        """Complete the task with a status message (no artifact created)."""
        msg_parts = [Part(TextPart(text=text))] if text is not None else []
        msg = self._make_agent_message(msg_parts)
        await self._terminal_transition(TaskState.completed, message=msg)

    async def reply_directly(self, text: str) -> None:
        """Return a Message directly without task tracking."""
        message = self._make_agent_message([Part(TextPart(text=text))])
        await self._terminal_transition(
            TaskState.completed,
            message=message,
            task_metadata={DIRECT_REPLY_KEY: message.message_id},
            pre_events=[DirectReply(message=message)],
        )

    async def send_status(self, message: str | None = None) -> None:
        """Emit an intermediate status update (state stays working)."""
        if self._turn_ended:
            logger.warning("send_status called after turn ended for task %s", self.task_id)
            return
        status_msg = None
        if message is not None:
            status_msg = self._make_agent_message([Part(TextPart(text=message))])

        # SSE first — streaming clients see status immediately
        await self._emit_status(TaskState.working, message=status_msg, final=False)

        # Flush pending artifacts together with the status write.
        # Intentionally omit state= to avoid duplicate stateTransitions
        # entries — we are already in working state.
        if not self._deferred_storage and status_msg is not None:
            pending = self._pending_artifacts
            self._pending_artifacts = []
            try:
                await self._versioned_update(
                    self.task_id,
                    status_message=status_msg,
                    artifacts=pending or None,
                )
            except ConcurrencyError:
                # Status-only write lost to concurrent modification — re-buffer
                # artifacts and continue. Don't kill the task for a progress update.
                self._pending_artifacts = pending + self._pending_artifacts
                logger.warning(
                    "Status write skipped (concurrent modification) for %s", self.task_id
                )
                return  # Don't update _last_flush — retry sooner
            except Exception:
                self._pending_artifacts = pending + self._pending_artifacts
                raise
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
        if self._turn_ended:
            logger.warning("emit_artifact called after turn ended for task %s", self.task_id)
            return
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
            metadata=metadata,
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
