"""Storage ABC, helpers, and exceptions."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, Self

from a2a_pydantic import v10
from pydantic import BaseModel, Field
from typing_extensions import TypeVar

if TYPE_CHECKING:
    import types

logger = logging.getLogger(__name__)

# Reserved, framework-internal metadata keys on v10.Task / v10.Message.
# v10.Task has no top-level tenant/createdAt fields — we stash them here.
# All keys are prefixed with ``_a2akit_`` so they can be bulk-stripped by
# ``_sanitize_task_for_client`` before leaving the framework boundary.
META_TENANT_KEY = "_a2akit_tenant"
META_CREATED_AT_KEY = "_a2akit_createdAt"
META_LAST_MODIFIED_KEY = "_a2akit_lastModified"

# v1.0 drops ``TaskState.unknown``. v1.0 terminal states are the same four
# (completed/canceled/failed/rejected) plus nothing else — ``auth_required``
# and ``input_required`` are INTERRUPTIONS, not terminals, per spec.
TERMINAL_STATES: set[v10.TaskState] = {
    v10.TaskState.task_state_completed,
    v10.TaskState.task_state_canceled,
    v10.TaskState.task_state_failed,
    v10.TaskState.task_state_rejected,
}


class TaskNotFoundError(Exception):
    """Raised when a referenced task does not exist."""


class TaskTerminalStateError(Exception):
    """Raised when an operation attempts to modify a terminal task."""


class TaskNotAcceptingMessagesError(Exception):
    """Raised when a task does not accept new user input in its current state."""

    def __init__(self, state: v10.TaskState | None = None) -> None:
        self.state = state
        super().__init__("Task is not accepting messages")


class TaskNotCancelableError(Exception):
    """Raised when a cancel is attempted on a task in a terminal state (A2A §3.1.5)."""


class UnsupportedOperationError(Exception):
    """Raised when an operation is not supported for the current task state."""


class ContextMismatchError(Exception):
    """Raised when message contextId doesn't match the task's contextId."""


class ContentTypeNotSupportedError(Exception):
    """Raised when an incoming message part has an incompatible MIME type (A2A -32005)."""

    def __init__(self, mime_type: str) -> None:
        self.mime_type = mime_type
        super().__init__(f"Incompatible content type: {mime_type}")


class InvalidAgentResponseError(Exception):
    """Raised when the agent produces an internally inconsistent response (A2A -32006)."""

    def __init__(self, detail: str = "Invalid agent response") -> None:
        self.detail = detail
        super().__init__(detail)


class ConcurrencyError(Exception):
    """Raised when expected_version doesn't match stored version."""

    def __init__(self, message: str, current_version: int | None = None) -> None:
        super().__init__(message)
        self.current_version = current_version


class ListTasksQuery(BaseModel):
    """Filter and pagination parameters for listing tasks."""

    context_id: str | None = None
    tenant: str | None = None
    status: v10.TaskState | None = None
    page_size: int = Field(default=50, ge=1, le=100)
    page_token: str | None = None
    history_length: int | None = None
    status_timestamp_after: str | None = None
    include_artifacts: bool = False


class ListTasksResult(BaseModel):
    """Paginated result from listing tasks."""

    tasks: list[v10.Task] = Field(default_factory=list)
    next_page_token: str = Field(default="", serialization_alias="nextPageToken")
    page_size: int = Field(default=50, serialization_alias="pageSize")
    total_size: int = Field(default=0, serialization_alias="totalSize")


ContextT = TypeVar("ContextT", default=Any)


@dataclass(frozen=True)
class ArtifactWrite:
    """Per-artifact write descriptor with individual append semantics.

    Replaces the flat ``append_artifact: bool`` parameter on ``update_task``
    which applied a single flag to all artifacts in the list.
    """

    artifact: v10.Artifact
    append: bool = False


def _coerce_v10_message(msg: Any) -> v10.Message:
    """Coerce a v0.3-shaped / a2a-sdk Message into a v10.Message.

    Legacy tests and user code still construct ``a2a.types.Message`` or
    ``a2a_pydantic.v03.Message`` objects and hand them to the v10-only
    storage layer. Rather than force every caller to migrate, this helper
    round-trips through JSON and uses the library's ``convert_to_v10``.
    Pass-through when the input is already a ``v10.Message``.
    """
    if isinstance(msg, v10.Message):
        return msg
    # Fast path: v03.Message → convert.
    try:
        from a2a_pydantic import convert_to_v10, v03
    except ImportError:  # pragma: no cover
        raise TypeError(f"Cannot coerce {type(msg).__name__} to v10.Message") from None
    if isinstance(msg, v03.Message):
        return convert_to_v10(msg)
    # Slow path: any other Pydantic model that can serialize to the v03 wire
    # shape (notably ``a2a.types.Message`` from the a2a-sdk package).
    if hasattr(msg, "model_dump"):
        try:
            payload = msg.model_dump(mode="json", by_alias=True, exclude_none=True)
            v03_msg = v03.Message.model_validate(payload)
            return convert_to_v10(v03_msg)
        except Exception as exc:
            raise TypeError(f"Cannot coerce {type(msg).__name__} to v10.Message: {exc}") from exc
    raise TypeError(f"Cannot coerce {type(msg).__name__} to v10.Message")


def _coerce_v10_messages(msgs: Any) -> list[v10.Message] | None:
    """Apply :func:`_coerce_v10_message` element-wise. ``None`` stays ``None``."""
    if msgs is None:
        return None
    return [_coerce_v10_message(m) for m in msgs]


def _coerce_v10_artifact(a: Any) -> v10.Artifact:
    """Coerce a v0.3-shaped Artifact into a v10.Artifact via JSON round-trip."""
    if isinstance(a, v10.Artifact):
        return a
    try:
        from a2a_pydantic import convert_to_v10, v03
    except ImportError:  # pragma: no cover
        raise TypeError(f"Cannot coerce {type(a).__name__} to v10.Artifact") from None
    if isinstance(a, v03.Artifact):
        return convert_to_v10(a)
    if hasattr(a, "model_dump"):
        try:
            payload = a.model_dump(mode="json", by_alias=True, exclude_none=True)
            v03_a = v03.Artifact.model_validate(payload)
            return convert_to_v10(v03_a)
        except Exception as exc:
            raise TypeError(f"Cannot coerce {type(a).__name__} to v10.Artifact: {exc}") from exc
    raise TypeError(f"Cannot coerce {type(a).__name__} to v10.Artifact")


def _build_transition_record(
    state: str,
    timestamp: str,
    status_message: v10.Message | None = None,
) -> dict[str, str]:
    """Build a state-transition record for ``metadata["stateTransitions"]``."""
    record: dict[str, str] = {"state": state, "timestamp": timestamp}
    if status_message:
        for part in status_message.parts or []:
            if part.text:
                record["messageText"] = part.text
                break
    return record


class Storage(ABC, Generic[ContextT]):
    """Abstract storage interface for A2A tasks.

    Storage handles CRUD and data-integrity constraints (terminal-state
    guard, optimistic concurrency).  Business rules (role enforcement,
    state-machine transitions, context matching) live in
    :class:`TaskManager`.

    Subclasses MUST implement 3 abstract methods:
        load_task, create_task, update_task

    Optional with sensible defaults:
        list_tasks, delete_task, delete_context, get_version,
        load_context, update_context

    **Consistency requirement:** Implementations MUST provide
    read-your-writes consistency.  A ``load_task()`` call following
    ``update_task()`` or ``create_task()`` on the same connection
    MUST reflect the preceding write.  For database backends with
    read replicas, this means reading from the primary after writes.
    """

    async def __aenter__(self) -> Self:
        """Enter the async context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> bool:
        """Exit the async context manager."""
        return False

    async def health_check(self) -> dict[str, Any]:
        """Check backend connectivity. Override for real checks."""
        return {"status": "ok"}

    @abstractmethod
    async def load_task(
        self,
        task_id: str,
        history_length: int | None = None,
        *,
        include_artifacts: bool = True,
    ) -> v10.Task | None: ...

    async def list_tasks(self, query: ListTasksQuery) -> ListTasksResult:
        """Return filtered and paginated tasks.

        Default implementation returns empty results.  Override in
        backends that support listing.
        """
        return ListTasksResult()

    @abstractmethod
    async def create_task(
        self,
        context_id: str,
        message: v10.Message,
        *,
        idempotency_key: str | None = None,
    ) -> v10.Task:
        """Create a brand-new task from an initial message.

        **Message contract:** Implementations MUST NOT mutate the input
        ``message`` object.  Use ``message.model_copy(update=...)`` to
        create a copy with ``task_id`` and ``context_id`` set for
        storage.  The caller (TaskManager) is responsible for binding
        these fields on the original message after this method returns.

        If ``idempotency_key`` is provided and a task with the same
        key **and** ``context_id`` already exists, return the existing
        task instead of creating a duplicate.  The key is scoped per
        context to avoid a global unique index on large tables.

        **Idempotency-key retention differs per backend:** SQL and
        InMemory backends keep the mapping for the lifetime of the task.
        RedisStorage expires it after ``redis_idempotency_ttl_s``
        (default 24h) — a retry with the same key after the TTL creates
        a **new** task.

        **Atomicity requirement:** DB backends MUST implement idempotency
        as an atomic operation. The recommended pattern is a UNIQUE
        constraint on ``(context_id, idempotency_key)`` combined with
        ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` (PostgreSQL)
        or equivalent. A SELECT-then-INSERT pattern is NOT sufficient
        because it has a TOCTOU race under concurrent requests.

        InMemoryStorage uses an O(N) scan which is acceptable for
        development but MUST NOT be used as a template for production
        backends.

        **Just-created marker:** When a genuinely new row is inserted
        (not an idempotent hit), implementations MUST set the transient
        metadata key ``_a2akit_just_created = True`` on the **returned**
        Task object.  This key MUST NOT be persisted — it is a signal
        to the caller (``TaskManager``) so the caller can distinguish
        "I just created this" from "I found an existing task via the
        idempotency key".  On an idempotent hit, the marker MUST NOT
        be present.  ``TaskManager`` pops the key immediately after
        reading it, and ``_sanitize_task_for_client`` strips any
        leftover ``_``-prefixed keys before serializing to clients.
        """

    @abstractmethod
    async def update_task(
        self,
        task_id: str,
        state: v10.TaskState | None = None,
        *,
        status_message: v10.Message | None = None,
        artifacts: list[ArtifactWrite] | None = None,
        messages: list[v10.Message] | None = None,
        task_metadata: dict[str, Any] | None = None,
        expected_version: int | None = None,
    ) -> int:
        """Persist state change, artifacts, and messages atomically.

        Business rules (role enforcement, context mismatch) are
        handled by :class:`TaskManager`.  Data-integrity constraints
        (terminal guard, OCC) are enforced here.

        **Message binding contract:** All ``Message`` objects in
        ``messages`` MUST have ``task_id`` and ``context_id`` set by
        the caller before this method is called.  Storage backends
        MUST NOT be responsible for filling these fields.

        When ``state`` is ``None`` the current state MUST be preserved
        (keep-current semantics) — useful for pure artifact or message
        appends without a state transition.

        When ``status_message`` is provided alongside a ``state``
        transition, it is stored in ``task.status.message`` so that
        polling clients (``tasks/get``, blocking ``message/send``)
        see the agent's message in the status object (A2A §9.4).
        Ignored when ``state`` is ``None``.

        Each :class:`ArtifactWrite` carries its own ``append`` flag so
        that callers can mix append and replace operations in a single
        call (e.g. append to artifact A while replacing artifact B).

        When ``task_metadata`` is provided, its key-value pairs are
        merged into the task's ``metadata`` dict.

        When ``expected_version`` is provided and doesn't match the
        stored version, raise a :class:`ConcurrencyError`.  All
        backends (including InMemory) MUST check this parameter.
        DB backends should implement this as
        ``UPDATE ... WHERE id = ? AND version = ?``.

        **Terminal-state guard:** Implementations MUST reject state
        transitions on tasks that are already in a terminal state
        (completed, canceled, failed, rejected) by raising
        :class:`TaskTerminalStateError`.  This prevents concurrent
        writers from corrupting terminal states (e.g. force-cancel
        and worker completing simultaneously).  Pure message or
        artifact appends without a state transition (``state=None``)
        are not affected by this guard.

        Implementations MUST ensure that all changes are applied as a
        single atomic operation.  If any part fails, no changes must be
        visible.  For database backends this means a single transaction.

        **Return value:** The new version number after the write.
        All backends (including InMemory) MUST return an ``int``
        so callers can use it for subsequent optimistic-concurrency
        writes.  Use ``load_task()`` for reading back complete
        task state.
        """

    # Optional cascade target. Bound by the server wiring after the
    # PushConfigStore is constructed. Subclass ``delete_task`` /
    # ``delete_context`` implementations MUST call
    # ``_cascade_push_delete_for_task`` / ``_cascade_push_delete_for_context``
    # after a successful deletion so push configs don't orphan in the DB.
    _push_store: Any = None

    def bind_push_store(self, push_store: Any) -> None:
        """Bind a PushConfigStore for cascade deletion.

        Called once by the server wiring after both storage and
        push_store are constructed. A later ``delete_task`` /
        ``delete_context`` will then cascade-remove any push configs
        attached to the deleted tasks.
        """
        self._push_store = push_store

    async def _cascade_push_delete_for_task(self, task_id: str) -> None:
        """Remove push configs attached to ``task_id`` if a store is bound.

        Swallows exceptions — cascade failure MUST NOT roll back the
        primary task deletion, which already succeeded.
        """
        store = self._push_store
        if store is None:
            return
        try:
            await store.delete_configs_for_task(task_id)
        except Exception:
            logger.exception("Push-config cascade delete failed for task %s", task_id)

    async def _cascade_push_delete_for_tasks(self, task_ids: list[str]) -> None:
        """Batch variant of :meth:`_cascade_push_delete_for_task`."""
        store = self._push_store
        if store is None or not task_ids:
            return
        for tid in task_ids:
            try:
                await store.delete_configs_for_task(tid)
            except Exception:
                logger.exception("Push-config cascade delete failed for task %s", tid)

    async def delete_task(self, task_id: str) -> bool:
        """Delete a task by ID. Returns True if the task existed."""
        raise NotImplementedError

    async def delete_context(self, context_id: str) -> int:
        """Delete all tasks in a context. Returns the number of deleted tasks."""
        raise NotImplementedError

    async def get_version(self, task_id: str) -> int | None:
        """Return current optimistic-concurrency version for a task.

        Returns ``None`` when the backend does not support versioning
        or when ``task_id`` does not exist.  Default implementation
        returns ``None``.
        """
        return None

    async def load_context(self, context_id: str) -> ContextT | None:
        """Load stored context for a context_id. Returns None if not found.

        Default implementation returns None (no context storage).
        """
        return None

    async def update_context(self, context_id: str, context: ContextT) -> None:
        """Store context for a context_id.

        Default implementation is a no-op.
        """
