"""Storage ABC, helpers, and exceptions."""

from __future__ import annotations

import logging
import types
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Generic, Self

from typing_extensions import TypeVar

from a2a.types import Artifact, Message, Task, TaskState
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

TERMINAL_STATES: set[TaskState] = {
    TaskState.completed,
    TaskState.canceled,
    TaskState.failed,
    TaskState.rejected,
}


class TaskNotFoundError(Exception):
    """Raised when a referenced task does not exist."""


class TaskTerminalStateError(Exception):
    """Raised when an operation attempts to modify a terminal task."""


class TaskNotAcceptingMessagesError(Exception):
    """Raised when a task does not accept new user input in its current state."""

    def __init__(self, state: TaskState | None = None) -> None:
        self.state = state
        super().__init__("Task is not accepting messages")


class TaskNotCancelableError(Exception):
    """Raised when a cancel is attempted on a task in a terminal state (A2A §3.1.5)."""


class UnsupportedOperationError(Exception):
    """Raised when an operation is not supported for the current task state."""


class ContextMismatchError(Exception):
    """Raised when message contextId doesn't match the task's contextId."""


class ConcurrencyError(Exception):
    """Raised when expected_version doesn't match stored version."""


class ListTasksQuery(BaseModel):
    """Filter and pagination parameters for listing tasks."""

    context_id: str | None = None
    status: TaskState | None = None
    page_size: int = Field(default=50, ge=1, le=100)
    page_token: str | None = None
    history_length: int | None = None
    status_timestamp_after: str | None = None
    include_artifacts: bool = False


class ListTasksResult(BaseModel):
    """Paginated result from listing tasks."""

    tasks: list[Task] = Field(default_factory=list)
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

    artifact: Artifact
    append: bool = False


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

    @abstractmethod
    async def load_task(
        self,
        task_id: str,
        history_length: int | None = None,
        *,
        include_artifacts: bool = True,
    ) -> Task | None: ...

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
        message: Message,
        *,
        idempotency_key: str | None = None,
    ) -> Task:
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

        **Atomicity requirement:** DB backends MUST implement idempotency
        as an atomic operation. The recommended pattern is a UNIQUE
        constraint on ``(context_id, idempotency_key)`` combined with
        ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` (PostgreSQL)
        or equivalent. A SELECT-then-INSERT pattern is NOT sufficient
        because it has a TOCTOU race under concurrent requests.

        InMemoryStorage uses an O(N) scan which is acceptable for
        development but MUST NOT be used as a template for production
        backends.
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
