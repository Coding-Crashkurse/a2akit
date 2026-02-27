"""Storage ABC, helpers, and exceptions."""

from __future__ import annotations

import logging
import types
from abc import ABC, abstractmethod
from typing import Any, Generic, Self

from typing_extensions import TypeVar

from a2a.types import Artifact, Message, Role, Task, TaskState
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


class ContextMismatchError(Exception):
    """Raised when message contextId doesn't match the task's contextId."""


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
    next_page_token: str = ""
    page_size: int = 50
    total_size: int = 0


ContextT = TypeVar("ContextT", default=Any)


def _is_agent_role(role: str | Role | None) -> bool:
    """Check whether a role value represents the agent role."""
    if role is None:
        return False
    return role == "agent" or getattr(role, "value", None) == "agent"


class Storage(ABC, Generic[ContextT]):
    """Abstract storage interface for A2A tasks."""

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

    @staticmethod
    def _is_terminal(state: TaskState) -> bool:
        """Check whether a state is terminal."""
        return state in TERMINAL_STATES

    @staticmethod
    def _is_input_required(state: TaskState) -> bool:
        """Check whether a state is input-required."""
        return state is TaskState.input_required

    def _handle_terminal_update(
        self,
        current: TaskState,
        new_state: TaskState,
        artifacts: list[Artifact] | None,
        messages: list[Message] | None,
    ) -> bool:
        """Return True if the update is a no-op on a terminal task, raise if invalid."""
        if not self._is_terminal(current):
            return False
        if self._is_terminal(new_state) and not artifacts and not messages:
            return True
        raise TaskTerminalStateError("task is terminal")

    def _enforce_message_roles(
        self, current: TaskState, messages: list[Message]
    ) -> None:
        """Raise if non-agent messages are sent to a task not in input-required state."""
        if not messages:
            return
        if not self._is_input_required(current):
            if not all(_is_agent_role(getattr(m, "role", None)) for m in messages):
                raise TaskNotAcceptingMessagesError(current)

    @abstractmethod
    async def load_task(
        self, task_id: str, history_length: int | None = None
    ) -> Task | None: ...

    @abstractmethod
    async def list_tasks(self, query: ListTasksQuery) -> ListTasksResult: ...

    @abstractmethod
    async def create_task(self, context_id: str, message: Message) -> Task:
        """Create a brand-new task from an initial message."""

    @abstractmethod
    async def append_message(self, task_id: str, message: Message) -> Task:
        """Append a follow-up message to an existing task."""

    async def submit_task(self, context_id: str, message: Message) -> Task:
        """Route to create_task or append_message based on message.task_id."""
        if message.task_id:
            return await self.append_message(message.task_id, message)
        return await self.create_task(context_id, message)

    @abstractmethod
    async def transition_state(self, task_id: str, state: TaskState) -> Task:
        """Transition a task to a new state."""

    @abstractmethod
    async def append_messages(self, task_id: str, messages: list[Message]) -> Task:
        """Append messages to a task's history."""

    @abstractmethod
    async def upsert_artifact(
        self, task_id: str, artifact: Artifact, *, append: bool = False
    ) -> Task:
        """Insert or update an artifact on a task.

        If append=True and an artifact with the same artifact_id exists,
        extend its parts. Otherwise replace or insert.
        """

    async def update_task(
        self,
        task_id: str,
        state: TaskState,
        *,
        artifacts: list[Artifact] | None = None,
        messages: list[Message] | None = None,
        append_artifact: bool = False,
    ) -> Task:
        """Convenience method that calls append_messages, upsert_artifact, then transition_state.

        Messages and artifacts are persisted before transitioning state so that
        a terminal state transition does not block their insertion.

        Important: append_messages and upsert_artifact will raise TaskTerminalStateError
        if the task is already in a terminal state. Only call this method on tasks that
        are in a non-terminal state, or without messages/artifacts if the task may be terminal.
        """
        task: Task | None = None
        if messages:
            task = await self.append_messages(task_id, messages)
        if artifacts:
            for artifact in artifacts:
                task = await self.upsert_artifact(
                    task_id, artifact, append=append_artifact
                )
        task = await self.transition_state(task_id, state)
        return task

    async def delete_task(self, task_id: str) -> bool:
        """Delete a task by ID. Returns True if the task existed."""
        raise NotImplementedError

    async def delete_context(self, context_id: str) -> int:
        """Delete all tasks in a context. Returns the number of deleted tasks."""
        raise NotImplementedError

    async def load_context(self, context_id: str) -> ContextT | None:
        """Load stored context for a context_id. Returns None if not found.

        Default implementation returns None (no context storage).
        """
        return None

    async def update_context(self, context_id: str, context: ContextT) -> None:
        """Store context for a context_id.

        Default implementation is a no-op.
        """
