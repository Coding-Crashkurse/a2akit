"""Shared SQLAlchemy base for SQL storage backends (PostgreSQL, SQLite)."""

from __future__ import annotations

import json
import logging
import uuid
from abc import abstractmethod
from datetime import UTC, datetime
from typing import Any

from a2a_pydantic import v10
from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, Text, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

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
        message: v10.Message,
        idempotency_key: str,
    ) -> v10.Task | None:
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
    def _serialize_message(msg: v10.Message | None) -> str | None:
        if msg is None:
            return None
        out: str = msg.model_dump_json(by_alias=True, exclude_none=True)
        return out

    @staticmethod
    def _deserialize_message(data: str | None) -> v10.Message | None:
        if not data:
            return None
        return v10.Message.model_validate_json(data)

    @staticmethod
    def _serialize_messages(msgs: list[v10.Message]) -> str:
        return json.dumps(
            [m.model_dump(mode="json", by_alias=True, exclude_none=True) for m in msgs]
        )

    @staticmethod
    def _deserialize_messages(data: str) -> list[v10.Message]:
        raw = json.loads(data)
        return [v10.Message.model_validate(m) for m in raw]

    @staticmethod
    def _serialize_artifacts(artifacts: list[v10.Artifact]) -> str:
        return json.dumps(
            [a.model_dump(mode="json", by_alias=True, exclude_none=True) for a in artifacts]
        )

    @staticmethod
    def _deserialize_artifacts(data: str) -> list[v10.Artifact]:
        raw = json.loads(data)
        return [v10.Artifact.model_validate(a) for a in raw]

    def _row_to_task(
        self,
        row: Any,
        history_length: int | None = None,
        include_artifacts: bool = True,
    ) -> v10.Task:
        """Convert a database row to a Task object."""
        history = self._deserialize_messages(row.history)
        if history_length is not None:
            history = history[-history_length:] if history_length > 0 else []

        artifacts_list: list[v10.Artifact] = []
        if include_artifacts:
            artifacts_list = self._deserialize_artifacts(row.artifacts)

        metadata_raw = json.loads(row.metadata_json) if row.metadata_json else None

        status = v10.TaskStatus(
            state=v10.TaskState(row.status_state),
            timestamp=row.status_timestamp,
            message=self._deserialize_message(row.status_message),
        )

        return v10.Task(
            id=row.id,
            context_id=row.context_id,
            status=status,
            history=history,
            artifacts=artifacts_list,
            metadata=metadata_raw,
        )

    async def load_task(
        self,
        task_id: str,
        history_length: int | None = None,
        *,
        include_artifacts: bool = True,
    ) -> v10.Task | None:
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
        message: v10.Message,
        *,
        idempotency_key: str | None = None,
    ) -> v10.Task:
        # Compat: accept legacy v0.3 / a2a-sdk Messages.
        message = _coerce_v10_message(message)
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
                    # Idempotent hit: return without the just-created marker.
                    return existing
            else:
                initial_meta = {
                    "stateTransitions": [
                        _build_transition_record(v10.TaskState.task_state_submitted.value, now),
                    ],
                    META_CREATED_AT_KEY: now,
                    META_LAST_MODIFIED_KEY: now,
                }
                await session.execute(
                    tasks_table.insert().values(
                        id=task_id,
                        context_id=context_id,
                        status_state=v10.TaskState.task_state_submitted.value,
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
        if loaded is None:
            # Same failure mode as RedisStorage: don't rely on ``assert``
            # (stripped under ``python -O``) for a real integrity check.
            raise TaskNotFoundError(f"Task {task_id} vanished immediately after create")
        # Attach the transient just-created marker (see storage/base.py
        # contract). Not persisted — TaskManager pops it, and
        # _sanitize_task_for_client strips any leftover _-prefixed keys.
        # a2a-pydantic ≥0.0.6 coerces dict → Struct on assignment.
        loaded.metadata = {**(loaded.metadata or {}), "_a2akit_just_created": True}
        return loaded

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
        # Compat: coerce v0.3 / sdk-shaped inputs to v10.
        if status_message is not None:
            status_message = _coerce_v10_message(status_message)
        if messages:
            messages = _coerce_v10_messages(messages)
        if artifacts:
            artifacts = [
                ArtifactWrite(_coerce_v10_artifact(aw.artifact), append=aw.append)
                for aw in artifacts
            ]
        # The merge (history append, artifact apply, metadata merge) happens
        # in Python between a SELECT and a version-guarded UPDATE. The guard
        # hits 0 rows when another writer commits in that window. When the
        # caller pinned ``expected_version`` the conflict MUST surface as
        # ConcurrencyError (strict OCC). When the caller did a blind write
        # (``expected_version=None``) their semantics are "merge on top of
        # whatever is current", so we transparently re-read and re-merge —
        # bounded, mirroring RedisStorage's retry loop.
        max_attempts = 1 if expected_version is not None else 8
        for attempt in range(max_attempts):
            async with self._get_session() as session, session.begin():
                result = await session.execute(
                    tasks_table.select().where(tasks_table.c.id == task_id)
                )
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

                now_iso = datetime.now(UTC).isoformat()
                if state is not None:
                    values["status_state"] = state.value
                    values["status_timestamp"] = now_iso
                    values["status_message"] = self._serialize_message(status_message)
                    # Append state-transition record (after task_metadata merge)
                    existing_meta = json.loads(
                        values.get("metadata_json") or row.metadata_json or "{}"
                    )
                    existing_meta.setdefault("stateTransitions", []).append(
                        _build_transition_record(state.value, now_iso, status_message),
                    )
                    existing_meta[META_LAST_MODIFIED_KEY] = now_iso
                    values["metadata_json"] = json.dumps(existing_meta)
                elif status_message is not None:
                    # Update status message without a state transition (e.g. progress text)
                    values["status_message"] = self._serialize_message(status_message)
                    values["status_timestamp"] = now_iso
                    existing_meta = json.loads(
                        values.get("metadata_json") or row.metadata_json or "{}"
                    )
                    existing_meta[META_LAST_MODIFIED_KEY] = now_iso
                    values["metadata_json"] = json.dumps(existing_meta)

                result = await session.execute(
                    tasks_table.update()
                    .where(tasks_table.c.id == task_id)
                    .where(tasks_table.c.version == row.version)
                    .values(**values)
                )
                if result.rowcount > 0:  # type: ignore[attr-defined]
                    return int(new_version)

            # rowcount == 0: concurrent writer bumped the version between
            # our SELECT and UPDATE.
            if expected_version is not None or attempt == max_attempts - 1:
                raise ConcurrencyError(
                    f"Concurrent modification of task {task_id}",
                    current_version=None,  # force fresh read via get_version()
                )
            # Blind write: retry with a fresh read so the caller's merge
            # lands on top of the winning writer's state.

        # Unreachable: loop either returns or raises
        raise RuntimeError("update_task retry loop exited without result")

    @staticmethod
    def _apply_artifact(
        existing: list[v10.Artifact], artifact: v10.Artifact, *, append: bool
    ) -> list[v10.Artifact]:
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

    def _tenant_condition(self, tenant: str) -> Any:
        """SQL predicate matching tasks whose metadata carries ``tenant``.

        There is no dedicated tenant column — the tenant lives inside the
        ``metadata_json`` TEXT column under ``META_TENANT_KEY``, so it is
        extracted with the dialect's JSON function. Rows with NULL or
        tenant-less metadata yield SQL NULL and never match. In heavily
        tenanted workloads a proper indexed column remains future work.
        """
        dialect = self._engine.dialect.name if self._engine is not None else ""
        if dialect == "postgresql":
            return (
                func.json_extract_path_text(
                    cast(tasks_table.c.metadata_json, JSON), META_TENANT_KEY
                )
                == tenant
            )
        # SQLite (and MySQL) provide json_extract() with a JSON-path argument.
        return func.json_extract(tasks_table.c.metadata_json, f'$."{META_TENANT_KEY}"') == tenant

    async def list_tasks(self, query: ListTasksQuery) -> ListTasksResult:
        async with self._get_session() as session:
            conditions = []
            if query.context_id:
                conditions.append(tasks_table.c.context_id == query.context_id)
            if query.status:
                conditions.append(tasks_table.c.status_state == query.status.value)
            if query.status_timestamp_after:
                conditions.append(tasks_table.c.status_timestamp > query.status_timestamp_after)
            if query.tenant:
                # Tenant filtering MUST happen in SQL, before COUNT and
                # pagination — post-filtering the fetched page in Python
                # overstates total_size and can return empty pages that
                # still carry a next_page_token.
                conditions.append(self._tenant_condition(query.tenant))

            count_q = select(func.count()).select_from(tasks_table)
            for cond in conditions:
                count_q = count_q.where(cond)
            count_result = await session.execute(count_q)
            total_size = count_result.scalar() or 0

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
            existed = bool(getattr(result, "rowcount", 0) > 0)
        if existed:
            await self._cascade_push_delete_for_task(task_id)
        return existed

    async def delete_context(self, context_id: str) -> int:
        async with self._get_session() as session, session.begin():
            # Capture affected task_ids BEFORE the delete so we can cascade
            # push-config cleanup after the transaction commits.
            id_rows = await session.execute(
                tasks_table.select()
                .with_only_columns(tasks_table.c.id)
                .where(tasks_table.c.context_id == context_id)
            )
            affected_task_ids = [row.id for row in id_rows]
            result = await session.execute(
                tasks_table.delete().where(tasks_table.c.context_id == context_id)
            )
            await session.execute(
                contexts_table.delete().where(contexts_table.c.context_id == context_id)
            )
            rowcount = int(getattr(result, "rowcount", 0))
        await self._cascade_push_delete_for_tasks(affected_task_ids)
        return rowcount

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
        # UPDATE-then-INSERT with a fallback UPDATE on IntegrityError. This
        # avoids the TOCTOU window of SELECT-then-INSERT/UPDATE: two
        # concurrent callers for the same context_id could both see "no row"
        # and both attempt INSERT, producing a primary-key violation.
        async with self._get_session() as session, session.begin():
            result = await session.execute(
                contexts_table.update()
                .where(contexts_table.c.context_id == context_id)
                .values(data=data, updated_at=now)
            )
            if getattr(result, "rowcount", 0) > 0:
                return
            try:
                await session.execute(
                    contexts_table.insert().values(
                        context_id=context_id, data=data, updated_at=now
                    )
                )
            except IntegrityError:
                # Another writer inserted the row between our UPDATE and
                # INSERT. Roll back this transaction so we can start a fresh
                # one for the retry UPDATE (the current transaction is
                # poisoned after the IntegrityError).
                await session.rollback()
                async with self._get_session() as retry_session, retry_session.begin():
                    await retry_session.execute(
                        contexts_table.update()
                        .where(contexts_table.c.context_id == context_id)
                        .values(data=data, updated_at=now)
                    )
