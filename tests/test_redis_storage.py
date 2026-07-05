"""Parametrized tests for Redis storage backend (with SQLite baseline)."""

from __future__ import annotations

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


def _ts(task) -> str:
    """Return a task's status timestamp as an ISO string."""
    ts = task.status.timestamp
    root = getattr(ts, "root", ts)
    return root.isoformat() if hasattr(root, "isoformat") else str(root)


def _msg(text: str = "hello", msg_id: str | None = None) -> Message:
    return Message(
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
        message_id=msg_id or str(uuid.uuid4()),
    )


def _artifact(artifact_id: str = "art-1", text: str = "content") -> Artifact:
    return Artifact(
        artifact_id=artifact_id,
        parts=[Part(root=TextPart(text=text))],
    )


# -- CRUD Basics --


async def test_create_and_load_task(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    assert task.id
    assert task.context_id == "ctx-1"
    assert task.status.state == TaskState.task_state_submitted

    loaded = await redis_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.id == task.id
    assert loaded.context_id == task.context_id
    assert loaded.status.state == task.status.state


async def test_load_nonexistent_returns_none(redis_storage):
    assert await redis_storage.load_task("does-not-exist") is None


async def test_create_task_idempotency(redis_storage):
    t1 = await redis_storage.create_task("ctx-1", _msg("a"), idempotency_key="idem-1")
    t2 = await redis_storage.create_task("ctx-1", _msg("b"), idempotency_key="idem-1")
    assert t1.id == t2.id


async def test_create_task_idempotency_different_context(redis_storage):
    t1 = await redis_storage.create_task("ctx-1", _msg("a"), idempotency_key="idem-1")
    t2 = await redis_storage.create_task("ctx-2", _msg("b"), idempotency_key="idem-1")
    assert t1.id != t2.id


async def test_create_task_without_idempotency_key(redis_storage):
    t1 = await redis_storage.create_task("ctx-1", _msg("a"))
    t2 = await redis_storage.create_task("ctx-1", _msg("b"))
    assert t1.id != t2.id


# -- update_task --


async def test_update_state_transition(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    await redis_storage.update_task(task.id, state=TaskState.task_state_working)
    loaded = await redis_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.status.state == TaskState.task_state_working

    await redis_storage.update_task(task.id, state=TaskState.task_state_completed)
    loaded = await redis_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.status.state == TaskState.task_state_completed


async def test_update_appends_messages(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg("first"))
    m2 = _msg("second")
    m2 = m2.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
    await redis_storage.update_task(task.id, messages=[m2])

    loaded = await redis_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.history is not None
    assert len(loaded.history) == 2


async def test_update_artifacts_replace(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    art = _artifact("art-1", "v1")
    await redis_storage.update_task(task.id, artifacts=[ArtifactWrite(artifact=art)])

    art2 = _artifact("art-1", "v2")
    await redis_storage.update_task(task.id, artifacts=[ArtifactWrite(artifact=art2)])

    loaded = await redis_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.artifacts is not None
    assert len(loaded.artifacts) == 1
    assert loaded.artifacts[0].parts[0].text == "v2"


async def test_update_artifacts_append(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    art = _artifact("art-1", "part1")
    await redis_storage.update_task(task.id, artifacts=[ArtifactWrite(artifact=art)])

    art2 = _artifact("art-1", "part2")
    await redis_storage.update_task(task.id, artifacts=[ArtifactWrite(artifact=art2, append=True)])

    loaded = await redis_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.artifacts is not None
    assert len(loaded.artifacts) == 1
    assert len(loaded.artifacts[0].parts) == 2


async def test_update_merges_metadata(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    await redis_storage.update_task(task.id, task_metadata={"key1": "val1"})
    await redis_storage.update_task(task.id, task_metadata={"key2": "val2"})

    loaded = await redis_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.metadata is not None
    assert loaded.metadata["key1"] == "val1"
    assert loaded.metadata["key2"] == "val2"


async def test_update_preserves_state_when_none(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    await redis_storage.update_task(task.id, state=TaskState.task_state_working)
    await redis_storage.update_task(task.id, task_metadata={"x": 1})

    loaded = await redis_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.status.state == TaskState.task_state_working


async def test_update_status_message_stored(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    status_msg = _msg("status update")
    await redis_storage.update_task(
        task.id, state=TaskState.task_state_working, status_message=status_msg
    )

    loaded = await redis_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.status.message is not None
    assert loaded.status.message.parts[0].text == "status update"


# -- Terminal-State-Guard --


async def test_terminal_state_blocks_transition(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    await redis_storage.update_task(task.id, state=TaskState.task_state_completed)

    with pytest.raises(TaskTerminalStateError):
        await redis_storage.update_task(task.id, state=TaskState.task_state_working)


async def test_terminal_state_allows_message_append(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    await redis_storage.update_task(task.id, state=TaskState.task_state_completed)

    m2 = _msg("append")
    m2 = m2.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
    version = await redis_storage.update_task(task.id, messages=[m2])
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
async def test_all_terminal_states_guarded(redis_storage, terminal_state):
    task = await redis_storage.create_task("ctx-1", _msg())
    await redis_storage.update_task(task.id, state=terminal_state)

    with pytest.raises(TaskTerminalStateError):
        await redis_storage.update_task(task.id, state=TaskState.task_state_working)


# -- OCC --


async def test_version_increments_on_update(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    v1 = await redis_storage.get_version(task.id)
    assert v1 == 1

    await redis_storage.update_task(task.id, state=TaskState.task_state_working)
    v2 = await redis_storage.get_version(task.id)
    assert v2 == 2


async def test_expected_version_match_succeeds(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    version = await redis_storage.update_task(
        task.id, state=TaskState.task_state_working, expected_version=1
    )
    assert version == 2


async def test_expected_version_mismatch_raises(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())

    with pytest.raises(ConcurrencyError):
        await redis_storage.update_task(
            task.id, state=TaskState.task_state_working, expected_version=999
        )


async def test_get_version_nonexistent_returns_none(redis_storage):
    assert await redis_storage.get_version("does-not-exist") is None


# -- list_tasks --


async def test_list_tasks_empty(redis_storage):
    result = await redis_storage.list_tasks(ListTasksQuery())
    assert result.tasks == []
    assert result.total_size == 0


async def test_list_tasks_filter_by_context(redis_storage):
    await redis_storage.create_task("ctx-1", _msg("a"))
    await redis_storage.create_task("ctx-2", _msg("b"))

    result = await redis_storage.list_tasks(ListTasksQuery(context_id="ctx-1"))
    assert len(result.tasks) == 1
    assert result.tasks[0].context_id == "ctx-1"


async def test_list_tasks_filter_by_status(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    await redis_storage.update_task(task.id, state=TaskState.task_state_working)
    await redis_storage.create_task("ctx-1", _msg())

    result = await redis_storage.list_tasks(ListTasksQuery(status=TaskState.task_state_working))
    assert len(result.tasks) == 1


async def test_list_tasks_pagination(redis_storage):
    for i in range(5):
        await redis_storage.create_task("ctx-1", _msg(f"msg-{i}"))

    page1 = await redis_storage.list_tasks(ListTasksQuery(page_size=2))
    assert len(page1.tasks) == 2
    assert page1.total_size == 5
    assert page1.next_page_token != ""

    page2 = await redis_storage.list_tasks(
        ListTasksQuery(page_size=2, page_token=page1.next_page_token)
    )
    assert len(page2.tasks) == 2


async def test_list_tasks_history_length(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg("first"))
    m2 = _msg("second")
    m2 = m2.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
    await redis_storage.update_task(task.id, messages=[m2])

    result = await redis_storage.list_tasks(ListTasksQuery(history_length=1))
    assert len(result.tasks) == 1
    assert result.tasks[0].history is not None
    assert len(result.tasks[0].history) == 1


async def test_list_tasks_exclude_artifacts(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    await redis_storage.update_task(task.id, artifacts=[ArtifactWrite(artifact=_artifact())])

    result = await redis_storage.list_tasks(ListTasksQuery(include_artifacts=False))
    assert not result.tasks[0].artifacts


async def test_list_tasks_status_timestamp_after(redis_storage):
    """statusTimestampAfter returns only tasks whose status changed after the cutoff."""
    t1 = await redis_storage.create_task("ctx-1", _msg("a"))
    cutoff = _ts(await redis_storage.load_task(t1.id))
    await anyio.sleep(0.01)  # ensure a strictly later timestamp
    t2 = await redis_storage.create_task("ctx-1", _msg("b"))

    result = await redis_storage.list_tasks(ListTasksQuery(status_timestamp_after=cutoff))
    assert [t.id for t in result.tasks] == [t2.id]
    assert result.total_size == 1


async def test_list_tasks_tenant_filter(redis_storage):
    """tenant filter matches only tasks whose metadata carries the tenant."""
    t1 = await redis_storage.create_task("ctx-1", _msg("a"))
    await redis_storage.update_task(t1.id, task_metadata={META_TENANT_KEY: "acme"})
    await redis_storage.create_task("ctx-1", _msg("b"))

    result = await redis_storage.list_tasks(ListTasksQuery(tenant="acme"))
    assert [t.id for t in result.tasks] == [t1.id]
    assert result.total_size == 1


# -- history_length --


async def test_load_task_full_history(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg("first"))
    m2 = _msg("second")
    m2 = m2.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
    await redis_storage.update_task(task.id, messages=[m2])

    loaded = await redis_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.history is not None
    assert len(loaded.history) == 2


async def test_load_task_trimmed_history(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg("first"))
    m2 = _msg("second")
    m2 = m2.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
    await redis_storage.update_task(task.id, messages=[m2])

    loaded = await redis_storage.load_task(task.id, history_length=1)
    assert loaded is not None
    assert loaded.history is not None
    assert len(loaded.history) == 1
    assert loaded.history[0].parts[0].text == "second"


async def test_load_task_history_length_zero(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg("first"))
    loaded = await redis_storage.load_task(task.id, history_length=0)
    assert loaded is not None
    assert loaded.history is None or loaded.history == []


async def test_load_task_exclude_artifacts(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    await redis_storage.update_task(task.id, artifacts=[ArtifactWrite(artifact=_artifact())])

    loaded = await redis_storage.load_task(task.id, include_artifacts=False)
    assert loaded is not None
    assert not loaded.artifacts


# -- delete --


async def test_delete_task_existing(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    assert await redis_storage.delete_task(task.id) is True
    assert await redis_storage.load_task(task.id) is None


async def test_delete_task_nonexistent(redis_storage):
    assert await redis_storage.delete_task("does-not-exist") is False


async def test_delete_context(redis_storage):
    await redis_storage.create_task("ctx-shared", _msg("a"))
    await redis_storage.create_task("ctx-shared", _msg("b"))

    count = await redis_storage.delete_context("ctx-shared")
    assert count == 2


# -- Context --


async def test_context_load_save_roundtrip(redis_storage):
    await redis_storage.update_context("ctx-1", {"counter": 42})
    loaded = await redis_storage.load_context("ctx-1")
    assert loaded == {"counter": 42}


async def test_context_load_nonexistent(redis_storage):
    assert await redis_storage.load_context("does-not-exist") is None


async def test_context_update_overwrites(redis_storage):
    await redis_storage.update_context("ctx-1", {"v": 1})
    await redis_storage.update_context("ctx-1", {"v": 2})
    loaded = await redis_storage.load_context("ctx-1")
    assert loaded == {"v": 2}


async def test_context_deleted_with_context(redis_storage):
    await redis_storage.update_context("ctx-1", {"data": True})
    await redis_storage.create_task("ctx-1", _msg())
    await redis_storage.delete_context("ctx-1")
    assert await redis_storage.load_context("ctx-1") is None


# -- Serialization --


async def test_message_roundtrip_preserves_fields(redis_storage):
    msg = _msg("test message")
    task = await redis_storage.create_task("ctx-1", msg)
    loaded = await redis_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.history is not None
    assert loaded.history[0].parts[0].text == "test message"
    assert loaded.history[0].role == V10Role.role_user


async def test_artifact_roundtrip_preserves_parts(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    art = _artifact("art-1", "content here")
    await redis_storage.update_task(task.id, artifacts=[ArtifactWrite(artifact=art)])

    loaded = await redis_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.artifacts is not None
    assert loaded.artifacts[0].artifact_id == "art-1"
    assert loaded.artifacts[0].parts[0].text == "content here"


async def test_metadata_roundtrip(redis_storage):
    task = await redis_storage.create_task("ctx-1", _msg())
    await redis_storage.update_task(task.id, task_metadata={"key": "value", "num": 42})

    loaded = await redis_storage.load_task(task.id)
    assert loaded is not None
    assert loaded.metadata is not None
    assert loaded.metadata["key"] == "value"
    assert loaded.metadata["num"] == 42


# -- update_task raises TaskNotFoundError --


async def test_update_nonexistent_raises(redis_storage):
    with pytest.raises(TaskNotFoundError):
        await redis_storage.update_task("does-not-exist", state=TaskState.task_state_working)
