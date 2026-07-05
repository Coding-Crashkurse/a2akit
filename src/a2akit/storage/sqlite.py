"""SQLite storage backend using aiosqlite."""

from __future__ import annotations

try:
    import aiosqlite  # noqa: F401 — required by SQLAlchemy aiosqlite driver
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
    from sqlalchemy.pool import StaticPool
except ImportError as _import_error:
    raise ImportError(
        "SQLite storage requires additional dependencies. "
        "Install them with: pip install a2akit[sqlite]"
    ) from _import_error

import json
from datetime import UTC, datetime
from typing import Any

from a2a_pydantic import v10

from a2akit.storage._sql_base import SQLStorageBase, metadata_obj
from a2akit.storage.base import (
    META_CREATED_AT_KEY,
    META_LAST_MODIFIED_KEY,
    _build_transition_record,
)


class SQLiteStorage(SQLStorageBase[Any]):
    """SQLite storage with aiosqlite and WAL mode.

    Args:
        url: Connection string, e.g. ``"sqlite+aiosqlite:///path/to/db.sqlite"``
             or ``"sqlite+aiosqlite://"`` for in-memory.
        echo: Enable SQL logging (default: False).
    """

    def __init__(self, url: str = "sqlite+aiosqlite://", *, echo: bool = False) -> None:
        super().__init__(url)
        self._echo = echo

    async def _create_engine(self) -> AsyncEngine:
        is_memory = ":memory:" in self._url or self._url.endswith("://")
        kwargs: dict[str, Any] = {"echo": self._echo}

        if is_memory:
            kwargs["connect_args"] = {"check_same_thread": False}
            kwargs["poolclass"] = StaticPool

        engine = create_async_engine(self._url, **kwargs)

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn: Any, _connection_record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            # Wait up to 5s for a competing writer's lock instead of
            # failing instantly with "database is locked".
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

        return engine

    async def _create_tables(self, engine: AsyncEngine) -> None:
        async with engine.begin() as conn:
            await conn.run_sync(metadata_obj.create_all)
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_tasks_idempotency "
                    "ON a2akit_tasks(context_id, idempotency_key) "
                    "WHERE idempotency_key IS NOT NULL"
                )
            )

    async def _insert_idempotent(
        self,
        session: AsyncSession,
        task_id: str,
        context_id: str,
        message: v10.Message,
        idempotency_key: str,
    ) -> v10.Task | None:
        now = datetime.now(UTC).isoformat()
        history = self._serialize_messages([message])
        metadata_json = json.dumps(
            {
                "_idempotency_key": idempotency_key,
                "stateTransitions": [
                    _build_transition_record(v10.TaskState.task_state_submitted.value, now)
                ],
                META_CREATED_AT_KEY: now,
                META_LAST_MODIFIED_KEY: now,
            }
        )

        await session.execute(
            text(
                "INSERT OR IGNORE INTO a2akit_tasks "
                "(id, context_id, status_state, status_timestamp, status_message, "
                "history, artifacts, metadata_json, version, idempotency_key, created_at) "
                "VALUES (:id, :context_id, :status_state, :status_timestamp, NULL, "
                ":history, '[]', :metadata_json, 1, :idempotency_key, :created_at)"
            ),
            {
                "id": task_id,
                "context_id": context_id,
                "status_state": v10.TaskState.task_state_submitted.value,
                "status_timestamp": now,
                "history": history,
                "metadata_json": metadata_json,
                "idempotency_key": idempotency_key,
                "created_at": now,
            },
        )

        # Fetch back — either our new row or the existing one
        result = await session.execute(
            text(
                "SELECT * FROM a2akit_tasks "
                "WHERE context_id = :context_id AND idempotency_key = :idempotency_key"
            ),
            {"context_id": context_id, "idempotency_key": idempotency_key},
        )
        row = result.first()
        if row is None:
            return None

        if row.id == task_id:
            return None  # New task — caller proceeds with load_task

        return self._row_to_task(row)
