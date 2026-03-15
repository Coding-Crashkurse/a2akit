"""PostgreSQL storage backend using asyncpg."""

from __future__ import annotations

try:
    import asyncpg  # noqa: F401 — required by SQLAlchemy asyncpg driver
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
except ImportError as _import_error:
    raise ImportError(
        "PostgreSQL storage requires additional dependencies. "
        "Install them with: pip install a2akit[postgres]"
    ) from _import_error

import json
from datetime import UTC, datetime
from typing import Any

from a2a.types import Message, Task, TaskState

from a2akit.storage._sql_base import SQLStorageBase, metadata_obj
from a2akit.storage.base import _build_transition_record


class PostgreSQLStorage(SQLStorageBase[Any]):
    """PostgreSQL storage with asyncpg and connection pooling.

    Args:
        url: Connection string, e.g. ``"postgresql+asyncpg://user:pass@host/db"``
        pool_size: Number of persistent connections (default: 5).
        max_overflow: Extra connections beyond pool_size (default: 10).
        pool_timeout: Seconds to wait for a free connection (default: 30).
        pool_recycle: Seconds before a connection is recycled (default: 3600).
        echo: Enable SQL logging (default: False).
    """

    def __init__(
        self,
        url: str,
        *,
        pool_size: int = 5,
        max_overflow: int = 10,
        pool_timeout: float = 30.0,
        pool_recycle: int = 3600,
        echo: bool = False,
    ) -> None:
        super().__init__(url)
        self._pool_size = pool_size
        self._max_overflow = max_overflow
        self._pool_timeout = pool_timeout
        self._pool_recycle = pool_recycle
        self._echo = echo

    async def _create_engine(self) -> AsyncEngine:
        return create_async_engine(
            self._url,
            pool_size=self._pool_size,
            max_overflow=self._max_overflow,
            pool_timeout=self._pool_timeout,
            pool_recycle=self._pool_recycle,
            echo=self._echo,
        )

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
        message: Message,
        idempotency_key: str,
    ) -> Task | None:
        now = datetime.now(UTC).isoformat()
        history = self._serialize_messages([message])
        metadata_json = json.dumps(
            {
                "_idempotency_key": idempotency_key,
                "stateTransitions": [_build_transition_record(TaskState.submitted.value, now)],
            }
        )

        await session.execute(
            text(
                "INSERT INTO a2akit_tasks "
                "(id, context_id, status_state, status_timestamp, status_message, "
                "history, artifacts, metadata_json, version, idempotency_key, created_at) "
                "VALUES (:id, :context_id, :status_state, :status_timestamp, NULL, "
                ":history, '[]', :metadata_json, 1, :idempotency_key, :created_at) "
                "ON CONFLICT (context_id, idempotency_key) "
                "WHERE idempotency_key IS NOT NULL "
                "DO NOTHING"
            ),
            {
                "id": task_id,
                "context_id": context_id,
                "status_state": TaskState.submitted.value,
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

        # Check if this is the row we just inserted
        if row.id == task_id:
            return None  # New task — caller proceeds with load_task

        return self._row_to_task(row)
