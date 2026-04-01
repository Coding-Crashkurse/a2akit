"""Shared SQLAlchemy base for SQL storage backends (PostgreSQL, SQLite)."""

from __future__ import annotations

import json
import logging
import uuid
from abc import abstractmethod
from datetime import UTC, datetime
from typing import Any

from a2a.types import Artifact, Message, Task, TaskState, TaskStatus
from sqlalchemy import Column, Integer, MetaData, String, Table, Text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from a2akit.storage.base import (
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
)

logger = logging.getLogger(__name__)

metadata_obj = MetaData()

tasks_table = Table(
    "a2akit_tasks",
    metadata_obj,
    Column("id", String(36), primary_key=True),
    Column("context_id", String(36), nullable=False, index=True),
    Column("status_state", String(20), nullable=False, index=True),
    Column("status_timestamp", Text, nullable=False),
    Column("status_message", Text, nullable=True),
    Column("history", Text, nullable=False, server_default="[]"),
    Column("artifacts", Text, nullable=False, server_default="[]"),
    Column("metadata_json", Text, nullable=True),
    Column("version", Integer, nullable=False, server_default="1"),
    Column("idempotency_key", String(255), nullable=True),
    Column("created_at", Text, nullable=False),
)

contexts_table = Table(
    "a2akit_contexts",
    metadata_obj,
    Column("context_id", String(36), primary_key=True),
    Column("data", Text, nullable=False),
    Column("updated_at", Text, nullable=False),
)


class SQLStorageBase(Storage[ContextT]):
    """Shared base for PostgreSQL and SQLite storage.

    Subclasses implement engine creation, table DDL, and idempotent insert.
    """

    def __init__(self, url: str, **engine_kwargs: Any) -> None:
        self._url = url
        self._engine_kwargs = engine_kwargs
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @abstractmethod
    async def _create_engine(self) -> AsyncEngine: ...

    @abstractmethod
    async def _create_tables(self, engine: AsyncEngine) -> None: ...

    @abstractmethod
    async def _insert_idempotent(
        self,
        session: AsyncSession,
        task_id: str,
        context_id: str,
        message: Message,
        idempotency_key: str,
    ) -> Task | None:
        """Idempotent INSERT. Returns existing task if key exists, else None."""

    async def __aenter__(self) -> SQLStorageBase[ContextT]:
        self._engine = await self._create_engine()
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        await self._create_tables(self._engine)
        return self

    async def __aexit__(self, *args: Any) -> bool:
        if self._engine:
            await self._engine.dispose()
        return False

    async def health_check(self) -> dict[str, Any]:
        """Execute ``SELECT 1`` to verify database connectivity."""
        try:
            if self._engine:
                from sqlalchemy import text

                async with self._engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    def _get_session(self) -> AsyncSession:
        if self._session_factory is None:
            msg = "Storage not entered — call async with storage first"
            raise RuntimeError(msg)
        return self._session_factory()

    @staticmethod
    def _serialize_message(msg: Message | None) -> str | None:
        if msg is None:
            return None
        return msg.model_dump_json(by_alias=True, exclude_none=True)

    @staticmethod
    def _deserialize_message(data: str | None) -> Message | None:
        if not data:
            return None
        return Message.model_validate_json(data)

    @staticmethod
    def _serialize_messages(msgs: list[Message]) -> str:
        return json.dumps(
            [m.model_dump(mode="json", by_alias=True, exclude_none=True) for m in msgs]
        )

    @staticmethod
    def _deserialize_messages(data: str) -> list[Message]:
        raw = json.loads(data)
        return [Message.model_validate(m) for m in raw]

    @staticmethod
    def _serialize_artifacts(artifacts: list[Artifact]) -> str:
        return json.dumps(
            [a.model_dump(mode="json", by_alias=True, exclude_none=True) for a in artifacts]
        )

    @staticmethod
    def _deserialize_artifacts(data: str) -> list[Artifact]:
        raw = json.loads(data)
        return [Artifact.model_validate(a) for a in raw]

    def _row_to_task(
        self,
        row: Any,
        history_length: int | None = None,
        include_artifacts: bool = True,
    ) -> Task:
        """Convert a database row to a Task object."""
        history = self._deserialize_messages(row.history)
        if history_length is not None:
            history = history[-history_length:] if history_length > 0 else []

        artifacts_list: list[Artifact] | None = None
        if include_artifacts:
            artifacts_list = self._deserialize_artifacts(row.artifacts)

        metadata_raw = json.loads(row.metadata_json) if row.metadata_json else None

        status = TaskStatus(
            state=TaskState(row.status_state),
            timestamp=row.status_timestamp,
            message=self._deserialize_message(row.status_message),
        )

        return Task(
            id=row.id,
            context_id=row.context_id,
            kind="task",
            status=status,
            history=history,
            artifacts=artifacts_list if artifacts_list else None,
            metadata=metadata_raw,
        )

    async def load_task(
        self,
        task_id: str,
        history_length: int | None = None,
        *,
        include_artifacts: bool = True,
    ) -> Task | None:
        async with self._get_session() as session:
            result = await session.execute(tasks_table.select().where(tasks_table.c.id == task_id))
            row = result.first()
            if row is None:
                return None
            return self._row_to_task(
                row, history_length=history_length, include_artifacts=include_artifacts
            )

    async def create_task(
        self,
        context_id: str,
        message: Message,
        *,
        idempotency_key: str | None = None,
    ) -> Task:
        task_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        history_msg = message.model_copy(update={"task_id": task_id, "context_id": context_id})

        async with self._get_session() as session, session.begin():
            if idempotency_key:
                existing = await self._insert_idempotent(
                    session,
                    task_id,
                    context_id,
                    history_msg,
                    idempotency_key,
                )
                if existing is not None:
                    return existing
            else:
                initial_meta = {
                    "stateTransitions": [
                        _build_transition_record(TaskState.submitted.value, now),
                    ],
                }
                await session.execute(
                    tasks_table.insert().values(
                        id=task_id,
                        context_id=context_id,
                        status_state=TaskState.submitted.value,
                        status_timestamp=now,
                        status_message=None,
                        history=self._serialize_messages([history_msg]),
                        artifacts="[]",
                        metadata_json=json.dumps(initial_meta),
                        version=1,
                        idempotency_key=None,
                        created_at=now,
                    )
                )

        loaded = await self.load_task(task_id)
        assert loaded is not None
        return loaded

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
        async with self._get_session() as session, session.begin():
            result = await session.execute(tasks_table.select().where(tasks_table.c.id == task_id))
            row = result.first()
            if row is None:
                raise TaskNotFoundError(f"Task {task_id} not found")

            if expected_version is not None and row.version != expected_version:
                raise ConcurrencyError(
                    f"Version mismatch for task {task_id}: "
                    f"expected {expected_version}, current {row.version}",
                    current_version=row.version,
                )

            if state is not None and row.status_state in {s.value for s in TERMINAL_STATES}:
                raise TaskTerminalStateError(
                    f"Cannot transition terminal task {task_id} "
                    f"from {row.status_state} to {state.value}"
                )

            values: dict[str, Any] = {}
            new_version = row.version + 1
            values["version"] = new_version

            if messages:
                existing_history = self._deserialize_messages(row.history)
                existing_history.extend(messages)
                values["history"] = self._serialize_messages(existing_history)

            if artifacts:
                existing_artifacts = self._deserialize_artifacts(row.artifacts)
                for aw in artifacts:
                    existing_artifacts = self._apply_artifact(
                        existing_artifacts, aw.artifact, append=aw.append
                    )
                values["artifacts"] = self._serialize_artifacts(existing_artifacts)

            if task_metadata:
                existing_meta = json.loads(row.metadata_json) if row.metadata_json else {}
                existing_meta.update(task_metadata)
                values["metadata_json"] = json.dumps(existing_meta)

            if state is not None:
                values["status_state"] = state.value
                values["status_timestamp"] = datetime.now(UTC).isoformat()
                values["status_message"] = self._serialize_message(status_message)
                # Append state-transition record (after task_metadata merge)
                existing_meta = json.loads(
                    values.get("metadata_json") or row.metadata_json or "{}"
                )
                existing_meta.setdefault("stateTransitions", []).append(
                    _build_transition_record(
                        state.value, values["status_timestamp"], status_message
                    ),
                )
                values["metadata_json"] = json.dumps(existing_meta)
            elif status_message is not None:
                # Update status message without a state transition (e.g. progress text)
                values["status_message"] = self._serialize_message(status_message)
                values["status_timestamp"] = datetime.now(UTC).isoformat()

            result = await session.execute(
                tasks_table.update()
                .where(tasks_table.c.id == task_id)
                .where(tasks_table.c.version == row.version)
                .values(**values)
            )
            if result.rowcount == 0:  # type: ignore[attr-defined]
                raise ConcurrencyError(
                    f"Concurrent modification of task {task_id}",
                    current_version=None,  # force fresh read via get_version()
                )

            return int(new_version)

    @staticmethod
    def _apply_artifact(
        existing: list[Artifact], artifact: Artifact, *, append: bool
    ) -> list[Artifact]:
        idx = next(
            (i for i, a in enumerate(existing) if a.artifact_id == artifact.artifact_id),
            None,
        )
        if idx is not None:
            if append:
                existing[idx].parts.extend(artifact.parts)
            else:
                existing[idx] = artifact
        else:
            existing.append(artifact)
        return existing

    async def list_tasks(self, query: ListTasksQuery) -> ListTasksResult:
        async with self._get_session() as session:
            conditions = []
            if query.context_id:
                conditions.append(tasks_table.c.context_id == query.context_id)
            if query.status:
                conditions.append(tasks_table.c.status_state == query.status.value)
            if query.status_timestamp_after:
                conditions.append(tasks_table.c.status_timestamp > query.status_timestamp_after)

            count_q = tasks_table.select()
            for cond in conditions:
                count_q = count_q.where(cond)
            count_result = await session.execute(count_q)
            total_size = len(count_result.fetchall())

            data_q = tasks_table.select()
            for cond in conditions:
                data_q = data_q.where(cond)
            try:
                offset = int(query.page_token) if query.page_token else 0
            except ValueError:
                offset = 0
            data_q = (
                data_q.order_by(tasks_table.c.status_timestamp.desc(), tasks_table.c.id.desc())
                .offset(offset)
                .limit(query.page_size)
            )

            result = await session.execute(data_q)
            rows = result.fetchall()

            tasks = [
                self._row_to_task(
                    r,
                    history_length=query.history_length,
                    include_artifacts=query.include_artifacts,
                )
                for r in rows
            ]

            next_offset = offset + query.page_size
            next_token = str(next_offset) if next_offset < total_size else ""

            return ListTasksResult(
                tasks=tasks,
                next_page_token=next_token,
                page_size=query.page_size,
                total_size=total_size,
            )

    async def delete_task(self, task_id: str) -> bool:
        async with self._get_session() as session, session.begin():
            result = await session.execute(tasks_table.delete().where(tasks_table.c.id == task_id))
            return bool(getattr(result, "rowcount", 0) > 0)

    async def delete_context(self, context_id: str) -> int:
        async with self._get_session() as session, session.begin():
            result = await session.execute(
                tasks_table.delete().where(tasks_table.c.context_id == context_id)
            )
            await session.execute(
                contexts_table.delete().where(contexts_table.c.context_id == context_id)
            )
            return int(getattr(result, "rowcount", 0))

    async def get_version(self, task_id: str) -> int | None:
        async with self._get_session() as session:
            result = await session.execute(
                tasks_table.select()
                .with_only_columns(tasks_table.c.version)
                .where(tasks_table.c.id == task_id)
            )
            row = result.first()
            return row.version if row else None

    async def load_context(self, context_id: str) -> ContextT | None:
        async with self._get_session() as session:
            result = await session.execute(
                contexts_table.select().where(contexts_table.c.context_id == context_id)
            )
            row = result.first()
            if row is None:
                return None
            return json.loads(row.data)  # type: ignore[no-any-return]

    async def update_context(self, context_id: str, context: ContextT) -> None:
        now = datetime.now(UTC).isoformat()
        data = json.dumps(context)
        async with self._get_session() as session, session.begin():
            result = await session.execute(
                contexts_table.select().where(contexts_table.c.context_id == context_id)
            )
            if result.first():
                await session.execute(
                    contexts_table.update()
                    .where(contexts_table.c.context_id == context_id)
                    .values(data=data, updated_at=now)
                )
            else:
                await session.execute(
                    contexts_table.insert().values(
                        context_id=context_id, data=data, updated_at=now
                    )
                )
