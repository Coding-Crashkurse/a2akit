"""In-memory storage backend for development and testing."""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from typing import Any

from a2a_pydantic import v10

from a2akit.storage.base import (
    META_CREATED_AT_KEY,
    META_LAST_MODIFIED_KEY,
    META_TENANT_KEY,
    TERMINAL_STATES,
    ArtifactWrite,
    ConcurrencyError,
    ContextT,
    ListTasksQuery,
    ListTasksResult,
    Storage,
    TaskNotFoundError,
    TaskTerminalStateError,
    _build_transition_record,
    _coerce_v10_artifact,
    _coerce_v10_message,
    _coerce_v10_messages,
)


class InMemoryStorage(Storage[ContextT]):
    """Simple in-memory storage for development and testing."""

    def __init__(self) -> None:
        """Initialize empty task and context stores."""
        self.tasks: dict[str, v10.Task] = {}
        self.contexts: dict[str, ContextT] = {}
        self._versions: dict[str, int] = {}

    @staticmethod
    def _trim_history(
        history: list[v10.Message] | None, history_length: int | None
    ) -> list[v10.Message] | None:
        """Trim history to the last N messages.

        Returns the full history when ``history_length`` is ``None``.
        Returns an empty list when ``history_length`` is ``0``.
        This avoids the Python falsy-check pitfall where ``0`` would
        previously skip trimming entirely.
        """
        if history_length is None or history is None:
            return history
        if history_length == 0:
            return []
        return history[-history_length:]

    async def load_task(
        self,
        task_id: str,
        history_length: int | None = None,
        *,
        include_artifacts: bool = True,
    ) -> v10.Task | None:
        """Load a task by ID, optionally trimming history."""
        task = self.tasks.get(task_id)
        if not task:
            return None
        t = copy.deepcopy(task)
        t.history = self._trim_history(t.history, history_length)
        if not include_artifacts:
            t.artifacts = []
        return t

    async def list_tasks(self, query: ListTasksQuery) -> ListTasksResult:
        """Return filtered and paginated tasks.

        v10 ``TaskStatus.timestamp`` is a Pydantic wrapper, not a plain
        string. Sort and filter on its ISO-8601 string form so comparisons
        stay total-orderable (the wrapper implements neither ``__lt__``
        against itself nor comparison against ``str``).
        """

        def _ts_str(t: v10.Task) -> str:
            ts = t.status.timestamp
            if ts is None:
                return ""
            root = getattr(ts, "root", ts)
            if isinstance(root, datetime):
                # isoformat() matches the wire/storage representation
                # ("...T..."); str(datetime) would use a space separator
                # and mis-order against client-supplied ISO strings.
                return root.isoformat()
            return str(root)

        def _sort_key(t: v10.Task) -> tuple[str, str]:
            return (_ts_str(t), t.id)

        all_tasks = sorted(self.tasks.values(), key=_sort_key, reverse=True)

        filtered: list[v10.Task] = []
        for t in all_tasks:
            if query.context_id and t.context_id != query.context_id:
                continue
            if query.status and t.status.state != query.status:
                continue
            if query.tenant and (t.metadata or {}).get(META_TENANT_KEY) != query.tenant:
                continue
            # Compare via the unwrapped string — the raw Timestamp wrapper
            # does not support ``<=`` against str (TypeError).
            if query.status_timestamp_after and _ts_str(t) <= query.status_timestamp_after:
                continue
            filtered.append(t)

        total_size = len(filtered)
        try:
            offset = int(query.page_token) if query.page_token else 0
        except ValueError:
            offset = 0
        page = filtered[offset : offset + query.page_size]

        results: list[v10.Task] = []
        for t in page:
            t = copy.deepcopy(t)
            t.history = self._trim_history(t.history, query.history_length)
            if not query.include_artifacts:
                t.artifacts = []
            results.append(t)

        next_offset = offset + query.page_size
        next_token = str(next_offset) if next_offset < total_size else ""

        return ListTasksResult(
            tasks=results,
            next_page_token=next_token,
            page_size=query.page_size,
            total_size=total_size,
        )

    async def create_task(
        self,
        context_id: str,
        message: v10.Message,
        *,
        idempotency_key: str | None = None,
    ) -> v10.Task:
        """Create a brand-new task from an initial message.

        If ``idempotency_key`` is provided and a task with that key
        already exists, return the existing task instead.  On a
        genuinely new insert the transient marker
        ``_a2akit_just_created=True`` is set on the returned object
        (not persisted) — see the ABC contract in ``storage/base.py``.
        """
        # Compat: accept v0.3 / a2a-sdk Message objects from legacy callers.
        message = _coerce_v10_message(message)
        if idempotency_key:
            for t in self.tasks.values():
                if (
                    t.context_id == context_id
                    and (t.metadata or {}).get("_idempotency_key") == idempotency_key
                ):
                    # Idempotent hit: return without the just-created marker.
                    return copy.deepcopy(t)

        task_id = str(uuid.uuid4())
        # Copy message for history — Storage MUST NOT mutate the input.
        history_msg = message.model_copy(update={"task_id": task_id, "context_id": context_id})

        now = datetime.now(UTC).isoformat()
        initial_meta: dict[str, Any] = {}
        if idempotency_key:
            initial_meta["_idempotency_key"] = idempotency_key
        initial_meta["stateTransitions"] = [
            _build_transition_record(v10.TaskState.task_state_submitted.value, now),
        ]
        initial_meta[META_CREATED_AT_KEY] = now
        initial_meta[META_LAST_MODIFIED_KEY] = now

        task = v10.Task(
            id=task_id,
            context_id=context_id,
            status=v10.TaskStatus(state=v10.TaskState.task_state_submitted, timestamp=now),
            history=[history_msg],
            artifacts=[],
            metadata=initial_meta,
        )
        self.tasks[task_id] = task
        self._versions[task_id] = 1
        # Return an independent copy — callers must not be able to mutate
        # our stored state by holding on to the returned object.  Attach
        # the transient just-created marker on the copy only so the stored
        # task stays clean.
        returned = copy.deepcopy(task)
        # ``validate_assignment=True`` on v10.Task coerces the dict back to a
        # Struct automatically (a2a-pydantic ≥0.0.6).
        returned.metadata = {**(returned.metadata or {}), "_a2akit_just_created": True}
        return returned

    def _get_task_or_raise(self, task_id: str) -> v10.Task:
        """Return the task or raise TaskNotFoundError."""
        task = self.tasks.get(task_id)
        if task is None:
            raise TaskNotFoundError("task not found")
        return task

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
        """Atomically apply messages, artifacts, and state transition.

        Business rules (role enforcement, context mismatch) are
        handled by :class:`TaskManager`.  Data-integrity constraints
        (terminal guard, OCC) are enforced here.

        When ``state`` is ``None`` the current state is preserved.
        Checks ``expected_version`` against an internal counter so
        that OCC logic in callers is exercised during development.
        Returns the new version after the write.
        """
        # Compat: coerce v0.3 / sdk-shaped inputs to v10 for legacy callers.
        if status_message is not None:
            status_message = _coerce_v10_message(status_message)
        if messages:
            messages = _coerce_v10_messages(messages)
        if artifacts:
            artifacts = [
                ArtifactWrite(_coerce_v10_artifact(aw.artifact), append=aw.append)
                for aw in artifacts
            ]
        task = self._get_task_or_raise(task_id)

        current_version = self._versions.get(task_id, 1)
        if expected_version is not None and expected_version != current_version:
            raise ConcurrencyError(
                f"Version mismatch for task {task_id}: "
                f"expected {expected_version}, current {current_version}",
                current_version=current_version,
            )

        if state is not None and task.status.state in TERMINAL_STATES:
            raise TaskTerminalStateError(
                f"Cannot transition terminal task {task_id} "
                f"from {task.status.state.value} to {state.value}"
            )

        # Atomicity: apply every change to a deep working copy and commit it
        # back only after all mutations succeed — a failure halfway (e.g. in
        # artifact application) must not leave a partially updated task.
        # Inputs are deep-copied as well so callers holding references to
        # the message/artifact objects cannot mutate stored state later.
        work = task.model_copy(deep=True)

        if messages:
            if work.history is None:
                work.history = []
            work.history.extend(m.model_copy(deep=True) for m in messages)

        if artifacts:
            for aw in artifacts:
                self._apply_artifact(work, aw.artifact.model_copy(deep=True), append=aw.append)

        if status_message is not None:
            status_message = status_message.model_copy(deep=True)

        # Always operate on a plain dict — v10.Task.metadata may be a Struct,
        # a dict, or None depending on history of reassignments.
        # Always work with a plain dict so setdefault / update have their
        # normal Python semantics; Pydantic's validate_assignment re-wraps
        # into Struct on write-back.
        md: dict[str, Any] = dict(work.metadata or {})

        if task_metadata:
            md.update(copy.deepcopy(task_metadata))

        now = datetime.now(UTC).isoformat()
        if state is not None:
            work.status = v10.TaskStatus(
                state=state,
                timestamp=now,
                message=status_message,
            )
            md.setdefault("stateTransitions", []).append(
                _build_transition_record(state.value, now, status_message),
            )
            md[META_LAST_MODIFIED_KEY] = now
        elif status_message is not None:
            # Update status message without a state transition (e.g. progress text)
            work.status = v10.TaskStatus(
                state=work.status.state,
                timestamp=now,
                message=status_message,
            )
            md[META_LAST_MODIFIED_KEY] = now

        work.metadata = md or None

        # Commit: all mutations succeeded — publish the working copy.
        self.tasks[task_id] = work
        new_version = current_version + 1
        self._versions[task_id] = new_version
        return new_version

    def _apply_artifact(self, task: v10.Task, artifact: v10.Artifact, *, append: bool) -> None:
        """Apply a single artifact upsert to the task (in-place)."""
        existing_idx = next(
            (i for i, a in enumerate(task.artifacts) if a.artifact_id == artifact.artifact_id),
            None,
        )
        if existing_idx is not None:
            if append:
                task.artifacts[existing_idx].parts.extend(artifact.parts)
            else:
                task.artifacts[existing_idx] = artifact
        else:
            task.artifacts.append(artifact)

    async def delete_task(self, task_id: str) -> bool:
        """Delete a task by ID. Returns True if the task existed."""
        self._versions.pop(task_id, None)
        existed = self.tasks.pop(task_id, None) is not None
        if existed:
            await self._cascade_push_delete_for_task(task_id)
        return existed

    async def delete_context(self, context_id: str) -> int:
        """Delete all tasks in a context. Returns the number of deleted tasks."""
        to_delete = [tid for tid, t in self.tasks.items() if t.context_id == context_id]
        for tid in to_delete:
            del self.tasks[tid]
            self._versions.pop(tid, None)
        self.contexts.pop(context_id, None)
        await self._cascade_push_delete_for_tasks(to_delete)
        return len(to_delete)

    async def get_version(self, task_id: str) -> int | None:
        """Return current OCC version for a task."""
        return self._versions.get(task_id)

    async def load_context(self, context_id: str) -> ContextT | None:
        """Load stored context for a context_id."""
        return self.contexts.get(context_id)

    async def update_context(self, context_id: str, context: ContextT) -> None:
        """Store context for a context_id."""
        self.contexts[context_id] = context
