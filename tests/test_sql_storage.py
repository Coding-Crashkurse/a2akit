"""Parametrized tests for SQL storage backends (SQLite + PostgreSQL)."""

from __future__ import annotations

import asyncio
import os
import uuid

import anyio
import pytest
from a2a.types import (
    Artifact,
    Message,
    Part,
    Role,
    TextPart,
)
from a2a_pydantic.v10 import Role as V10Role
from a2a_pydantic.v10 import TaskState

from a2akit.storage.base import (
    META_TENANT_KEY,
    ArtifactWrite,
    ConcurrencyError,
    ListTasksQuery,
    TaskNotFoundError,
    TaskTerminalStateError,
)

POSTGRES_URL = os.environ.get("A2AKIT_TEST_POSTGRES_URL")


def _msg(text: str = "hello", msg_id: str | None = None) -> Message:
    return Message(
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
        message_id=msg_id or str(uuid.uuid4()),
    )


def _ts(task) -> str:
    """Return a task's status timestamp as an ISO string."""
    ts = task.status.timestamp
    root = getattr(ts, "root", ts)
    return root.isoformat() if hasattr(root, "isoformat") else str(root)


def _sqlite_storage(url: str = "sqlite+aiosqlite://"):
    """Create a SQLiteStorage or skip when aiosqlite is missing."""
    try:
        from a2akit.storage.sqlite import SQLiteStorage
    except ImportError:
        pytest.skip("aiosqlite not installed")
    return SQLiteStorage(url)


def _artifact(artifact_id: str = "art-1", text: str = "content") -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        parts=[Part(root=TextPart(text=text))],
    )


# -- CRUD Basics --


async def test_create_and_load_task(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    assert task.id
    assert task.context_id == "ctx-1"
    assert task.status.state == TaskState.task_state_submitted

    loaded = await sql_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.id == task.id
    assert loaded.context_id == task.context_id
    assert loaded.status.state == task.status.state


async def test_load_nonexistent_returns_none(sql_storage):
    assert await sql_storage.load_task("does-not-exist") is None


async def test_create_task_idempotency(sql_storage):
    t1 = await sql_storage.create_task("ctx-1", _msg("a"), idempotency_key="idem-1")
    t2 = await sql_storage.create_task("ctx-1", _msg("b"), idempotency_key="idem-1")
    assert t1.id == t2.id


async def test_create_task_idempotency_different_context(sql_storage):
    t1 = await sql_storage.create_task("ctx-1", _msg("a"), idempotency_key="idem-1")
    t2 = await sql_storage.create_task("ctx-2", _msg("b"), idempotency_key="idem-1")
    assert t1.id != t2.id


async def test_create_task_without_idempotency_key(sql_storage):
    t1 = await sql_storage.create_task("ctx-1", _msg("a"))
    t2 = await sql_storage.create_task("ctx-1", _msg("b"))
    assert t1.id != t2.id


# -- update_task --


async def test_update_state_transition(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    await sql_storage.update_task(task.id, state=TaskState.task_state_working)
    loaded = await sql_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.status.state == TaskState.task_state_working

    await sql_storage.update_task(task.id, state=TaskState.task_state_completed)
    loaded = await sql_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.status.state == TaskState.task_state_completed


async def test_update_appends_messages(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg("first"))
    m2 = _msg("second")
    m2 = m2.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
    await sql_storage.update_task(task.id, messages=[m2])

    loaded = await sql_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.history is not None
    assert len(loaded.history) == 2


async def test_update_artifacts_replace(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    art = _artifact("art-1", "v1")
    await sql_storage.update_task(task.id, artifacts=[ArtifactWrite(artifact=art)])

    art2 = _artifact("art-1", "v2")
    await sql_storage.update_task(task.id, artifacts=[ArtifactWrite(artifact=art2)])

    loaded = await sql_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.artifacts is not None
    assert len(loaded.artifacts) == 1
    assert loaded.artifacts[0].parts[0].text == "v2"


async def test_update_artifacts_append(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    art = _artifact("art-1", "part1")
    await sql_storage.update_task(task.id, artifacts=[ArtifactWrite(artifact=art)])

    art2 = _artifact("art-1", "part2")
    await sql_storage.update_task(task.id, artifacts=[ArtifactWrite(artifact=art2, append=True)])

    loaded = await sql_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.artifacts is not None
    assert len(loaded.artifacts) == 1
    assert len(loaded.artifacts[0].parts) == 2


async def test_update_merges_metadata(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    await sql_storage.update_task(task.id, task_metadata={"key1": "val1"})
    await sql_storage.update_task(task.id, task_metadata={"key2": "val2"})

    loaded = await sql_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.metadata is not None
    assert loaded.metadata["key1"] == "val1"
    assert loaded.metadata["key2"] == "val2"


async def test_update_preserves_state_when_none(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    await sql_storage.update_task(task.id, state=TaskState.task_state_working)
    await sql_storage.update_task(task.id, task_metadata={"x": 1})

    loaded = await sql_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.status.state == TaskState.task_state_working


async def test_update_status_message_stored(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    status_msg = _msg("status update")
    await sql_storage.update_task(
        task.id, state=TaskState.task_state_working, status_message=status_msg
    )

    loaded = await sql_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.status.message is not None
    assert loaded.status.message.parts[0].text == "status update"


# -- Terminal-State-Guard --


async def test_terminal_state_blocks_transition(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    await sql_storage.update_task(task.id, state=TaskState.task_state_completed)

    with pytest.raises(TaskTerminalStateError):
        await sql_storage.update_task(task.id, state=TaskState.task_state_working)


async def test_terminal_state_allows_message_append(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    await sql_storage.update_task(task.id, state=TaskState.task_state_completed)

    m2 = _msg("append")
    m2 = m2.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
    version = await sql_storage.update_task(task.id, messages=[m2])
    assert version > 0


@pytest.mark.parametrize(
    "terminal_state",
    [
        TaskState.task_state_completed,
        TaskState.task_state_canceled,
        TaskState.task_state_failed,
        TaskState.task_state_rejected,
    ],
)
async def test_all_terminal_states_guarded(sql_storage, terminal_state):
    task = await sql_storage.create_task("ctx-1", _msg())
    await sql_storage.update_task(task.id, state=terminal_state)

    with pytest.raises(TaskTerminalStateError):
        await sql_storage.update_task(task.id, state=TaskState.task_state_working)


# -- OCC --


async def test_version_increments_on_update(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    v1 = await sql_storage.get_version(task.id)
    assert v1 == 1

    await sql_storage.update_task(task.id, state=TaskState.task_state_working)
    v2 = await sql_storage.get_version(task.id)
    assert v2 == 2


async def test_expected_version_match_succeeds(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    version = await sql_storage.update_task(
        task.id, state=TaskState.task_state_working, expected_version=1
    )
    assert version == 2


async def test_expected_version_mismatch_raises(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())

    with pytest.raises(ConcurrencyError):
        await sql_storage.update_task(
            task.id, state=TaskState.task_state_working, expected_version=999
        )


async def test_get_version_nonexistent_returns_none(sql_storage):
    assert await sql_storage.get_version("does-not-exist") is None


# -- list_tasks --


async def test_list_tasks_empty(sql_storage):
    result = await sql_storage.list_tasks(ListTasksQuery())
    assert result.tasks == []
    assert result.total_size == 0


async def test_list_tasks_filter_by_context(sql_storage):
    await sql_storage.create_task("ctx-1", _msg("a"))
    await sql_storage.create_task("ctx-2", _msg("b"))

    result = await sql_storage.list_tasks(ListTasksQuery(context_id="ctx-1"))
    assert len(result.tasks) == 1
    assert result.tasks[0].context_id == "ctx-1"


async def test_list_tasks_filter_by_status(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    await sql_storage.update_task(task.id, state=TaskState.task_state_working)
    await sql_storage.create_task("ctx-1", _msg())

    result = await sql_storage.list_tasks(ListTasksQuery(status=TaskState.task_state_working))
    assert len(result.tasks) == 1


async def test_list_tasks_pagination(sql_storage):
    for i in range(5):
        await sql_storage.create_task("ctx-1", _msg(f"msg-{i}"))

    page1 = await sql_storage.list_tasks(ListTasksQuery(page_size=2))
    assert len(page1.tasks) == 2
    assert page1.total_size == 5
    assert page1.next_page_token != ""

    page2 = await sql_storage.list_tasks(
        ListTasksQuery(page_size=2, page_token=page1.next_page_token)
    )
    assert len(page2.tasks) == 2


async def test_list_tasks_history_length(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg("first"))
    m2 = _msg("second")
    m2 = m2.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
    await sql_storage.update_task(task.id, messages=[m2])

    result = await sql_storage.list_tasks(ListTasksQuery(history_length=1))
    assert len(result.tasks) == 1
    assert result.tasks[0].history is not None
    assert len(result.tasks[0].history) == 1


async def test_list_tasks_exclude_artifacts(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    await sql_storage.update_task(task.id, artifacts=[ArtifactWrite(artifact=_artifact())])

    result = await sql_storage.list_tasks(ListTasksQuery(include_artifacts=False))
    assert not result.tasks[0].artifacts


async def test_list_tasks_status_timestamp_after(sql_storage):
    """statusTimestampAfter returns only tasks whose status changed after the cutoff."""
    t1 = await sql_storage.create_task("ctx-1", _msg("a"))
    cutoff = _ts(await sql_storage.load_task(t1.id))
    await anyio.sleep(0.01)  # ensure a strictly later timestamp
    t2 = await sql_storage.create_task("ctx-1", _msg("b"))

    result = await sql_storage.list_tasks(ListTasksQuery(status_timestamp_after=cutoff))
    assert [t.id for t in result.tasks] == [t2.id]
    assert result.total_size == 1


async def test_list_tasks_tenant_filter(sql_storage):
    """tenant filter matches only tasks whose metadata carries the tenant."""
    tenant_ids = []
    for i in range(3):
        t = await sql_storage.create_task("ctx-1", _msg(f"a{i}"))
        await sql_storage.update_task(t.id, task_metadata={META_TENANT_KEY: "acme"})
        tenant_ids.append(t.id)
    other = await sql_storage.create_task("ctx-1", _msg("other"))
    await sql_storage.update_task(other.id, task_metadata={META_TENANT_KEY: "globex"})
    await sql_storage.create_task("ctx-1", _msg("untenanted"))

    result = await sql_storage.list_tasks(ListTasksQuery(tenant="acme"))
    assert result.total_size == 3
    assert {t.id for t in result.tasks} == set(tenant_ids)


async def test_list_tasks_tenant_pagination(sql_storage):
    """total_size and next_page_token are computed on the tenant-filtered set."""
    tenant_ids = set()
    for i in range(3):
        t = await sql_storage.create_task("ctx-1", _msg(f"a{i}"))
        await sql_storage.update_task(t.id, task_metadata={META_TENANT_KEY: "acme"})
        tenant_ids.add(t.id)
    for i in range(3):
        await sql_storage.create_task("ctx-1", _msg(f"b{i}"))

    page1 = await sql_storage.list_tasks(ListTasksQuery(tenant="acme", page_size=2))
    assert len(page1.tasks) == 2
    assert page1.total_size == 3
    assert page1.next_page_token != ""

    page2 = await sql_storage.list_tasks(
        ListTasksQuery(tenant="acme", page_size=2, page_token=page1.next_page_token)
    )
    assert len(page2.tasks) == 1
    assert page2.next_page_token == ""
    assert {t.id for t in page1.tasks} | {t.id for t in page2.tasks} == tenant_ids


# -- history_length --


async def test_load_task_full_history(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg("first"))
    m2 = _msg("second")
    m2 = m2.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
    await sql_storage.update_task(task.id, messages=[m2])

    loaded = await sql_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.history is not None
    assert len(loaded.history) == 2


async def test_load_task_trimmed_history(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg("first"))
    m2 = _msg("second")
    m2 = m2.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
    await sql_storage.update_task(task.id, messages=[m2])

    loaded = await sql_storage.load_task(task.id, history_length=1)
    assert loaded is not None
    assert loaded.history is not None
    assert len(loaded.history) == 1
    assert loaded.history[0].parts[0].text == "second"


async def test_load_task_history_length_zero(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg("first"))
    loaded = await sql_storage.load_task(task.id, history_length=0)
    assert loaded is not None
    # history_length=0 means empty history
    assert loaded.history is None or loaded.history == []


async def test_load_task_exclude_artifacts(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    await sql_storage.update_task(task.id, artifacts=[ArtifactWrite(artifact=_artifact())])

    loaded = await sql_storage.load_task(task.id, include_artifacts=False)
    assert loaded is not None
    assert not loaded.artifacts


# -- delete --


async def test_delete_task_existing(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    assert await sql_storage.delete_task(task.id) is True
    assert await sql_storage.load_task(task.id) is None


async def test_delete_task_nonexistent(sql_storage):
    assert await sql_storage.delete_task("does-not-exist") is False


async def test_delete_context(sql_storage):
    await sql_storage.create_task("ctx-shared", _msg("a"))
    await sql_storage.create_task("ctx-shared", _msg("b"))

    count = await sql_storage.delete_context("ctx-shared")
    assert count == 2


# -- Context --


async def test_context_load_save_roundtrip(sql_storage):
    await sql_storage.update_context("ctx-1", {"counter": 42})
    loaded = await sql_storage.load_context("ctx-1")
    assert loaded == {"counter": 42}


async def test_context_load_nonexistent(sql_storage):
    assert await sql_storage.load_context("does-not-exist") is None


async def test_context_update_overwrites(sql_storage):
    await sql_storage.update_context("ctx-1", {"v": 1})
    await sql_storage.update_context("ctx-1", {"v": 2})
    loaded = await sql_storage.load_context("ctx-1")
    assert loaded == {"v": 2}


async def test_context_deleted_with_context(sql_storage):
    await sql_storage.update_context("ctx-1", {"data": True})
    await sql_storage.create_task("ctx-1", _msg())
    await sql_storage.delete_context("ctx-1")
    assert await sql_storage.load_context("ctx-1") is None


# -- Serialization --


async def test_message_roundtrip_preserves_fields(sql_storage):
    msg = _msg("test message")
    task = await sql_storage.create_task("ctx-1", msg)
    loaded = await sql_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.history is not None
    assert loaded.history[0].parts[0].text == "test message"
    assert loaded.history[0].role == V10Role.role_user


async def test_artifact_roundtrip_preserves_parts(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    art = _artifact("art-1", "content here")
    await sql_storage.update_task(task.id, artifacts=[ArtifactWrite(artifact=art)])

    loaded = await sql_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.artifacts is not None
    assert loaded.artifacts[0].artifact_id == "art-1"
    assert loaded.artifacts[0].parts[0].text == "content here"


async def test_metadata_roundtrip(sql_storage):
    task = await sql_storage.create_task("ctx-1", _msg())
    await sql_storage.update_task(task.id, task_metadata={"key": "value", "num": 42})

    loaded = await sql_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.metadata is not None
    assert loaded.metadata["key"] == "value"
    assert loaded.metadata["num"] == 42


# -- update_task raises TaskNotFoundError --


async def test_update_nonexistent_raises(sql_storage):
    with pytest.raises(TaskNotFoundError):
        await sql_storage.update_task("does-not-exist", state=TaskState.task_state_working)


# -- OCC contention (SQLite-specific: injects a competing write between
# -- the SELECT and the version-guarded UPDATE inside update_task) --


def _bump_version_sync(db_path, task_id: str) -> None:
    """Simulate a concurrent writer: bump the row version out-of-band."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            "UPDATE a2akit_tasks SET version = version + 1 WHERE id = ?",
            (task_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _install_conflict_hook(storage, db_path, task_id: str) -> None:
    """Bump the version during the first serialize call of update_task.

    ``_serialize_messages`` runs after update_task's SELECT and before its
    guarded UPDATE, so this deterministically forces the rowcount-0 branch
    on the first attempt only.
    """
    original = storage._serialize_messages
    fired = False

    def racy_serialize(msgs):
        nonlocal fired
        if not fired:
            fired = True
            _bump_version_sync(db_path, task_id)
        return original(msgs)

    storage._serialize_messages = racy_serialize  # instance attr shadows staticmethod


async def test_blind_write_retries_on_conflict(tmp_path):
    """A blind update_task (expected_version=None) retries past a conflicting writer."""
    db_path = tmp_path / "occ.sqlite"
    storage = _sqlite_storage(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    async with storage:
        task = await storage.create_task("ctx-1", _msg("first"))
        _install_conflict_hook(storage, db_path, task.id)

        m2 = _msg("second").model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
        version = await storage.update_task(task.id, messages=[m2])

        # v1 create → v2 competing bump → v3 our (retried) write
        assert version == 3
        loaded = await storage.load_task(task.id)
        assert loaded is not None
        assert len(loaded.history) == 2


async def test_expected_version_conflict_raises(tmp_path):
    """With expected_version pinned, a conflicting writer surfaces as ConcurrencyError."""
    db_path = tmp_path / "occ_strict.sqlite"
    storage = _sqlite_storage(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    async with storage:
        task = await storage.create_task("ctx-1", _msg("first"))
        _install_conflict_hook(storage, db_path, task.id)

        m2 = _msg("second").model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
        with pytest.raises(ConcurrencyError):
            await storage.update_task(task.id, messages=[m2], expected_version=1)


async def test_concurrent_blind_updates_all_succeed(tmp_path):
    """Concurrent blind update_task calls must all succeed (no spurious ConcurrencyError)."""
    db_path = tmp_path / "concurrent.sqlite"
    storage = _sqlite_storage(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    async with storage:
        task = await storage.create_task("ctx-1", _msg("first"))

        async def _append(i: int) -> None:
            m = _msg(f"msg-{i}").model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
            await storage.update_task(task.id, messages=[m])

        await asyncio.gather(*(_append(i) for i in range(4)))

        loaded = await storage.load_task(task.id)
        assert loaded is not None
        assert len(loaded.history) == 5  # initial + 4 appends
        assert await storage.get_version(task.id) == 5


async def test_concurrent_idempotent_create(tmp_path):
    """Concurrent create_task calls with the same idempotency key yield one task."""
    db_path = tmp_path / "idem.sqlite"
    storage = _sqlite_storage(f"sqlite+aiosqlite:///{db_path.as_posix()}")
    async with storage:
        t1, t2 = await asyncio.gather(
            storage.create_task("ctx-1", _msg("a"), idempotency_key="idem-1"),
            storage.create_task("ctx-1", _msg("b"), idempotency_key="idem-1"),
        )
        assert t1.id == t2.id


# -- SQLite connection pragmas --


async def test_sqlite_busy_timeout_pragma():
    """The connect hook sets busy_timeout so concurrent writers wait, not fail."""
    from sqlalchemy import text

    storage = _sqlite_storage()
    async with storage, storage._get_session() as session:
        result = await session.execute(text("PRAGMA busy_timeout"))
        assert result.scalar() == 5000
