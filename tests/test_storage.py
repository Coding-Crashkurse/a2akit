"""Unit tests for InMemoryStorage CRUD operations."""

from __future__ import annotations

import anyio
import pytest
from a2a.types import Artifact, Message, Part, Role, TextPart
from a2a_pydantic.v10 import TaskState

from a2akit.storage.base import (
    META_TENANT_KEY,
    ArtifactWrite,
    ConcurrencyError,
    ListTasksQuery,
    TaskTerminalStateError,
)


def _msg(text: str = "hello", msg_id: str = "msg1") -> Message:
    """Create a simple user message."""
    return Message(
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
        message_id=msg_id,
    )


def _ts(task) -> str:
    """Return a task's status timestamp as an ISO string."""
    ts = task.status.timestamp
    root = getattr(ts, "root", ts)
    return root.isoformat() if hasattr(root, "isoformat") else str(root)


async def test_create_task(storage):
    """Creating a task returns a Task with id, contextId, and submitted state."""
    task = await storage.create_task("ctx-1", _msg())

    assert task.id, "Task must have a non-empty id"
    assert task.context_id == "ctx-1"
    assert task.status.state == TaskState.task_state_submitted


async def test_load_task(storage):
    """A created task can be loaded back by id with identical data."""
    created = await storage.create_task("ctx-1", _msg())
    loaded = await storage.load_task(created.id)

    assert loaded is not None
    assert loaded.id == created.id
    assert loaded.context_id == created.context_id
    assert loaded.status.state == created.status.state


async def test_load_task_not_found(storage):
    """Loading a nonexistent task returns None."""
    result = await storage.load_task("does-not-exist")
    assert result is None


async def test_update_task_state(storage):
    """Updating a task's state persists the new state."""
    task = await storage.create_task("ctx-1", _msg())
    await storage.update_task(task.id, state=TaskState.task_state_working)

    loaded = await storage.load_task(task.id)
    assert loaded is not None
    assert loaded.status.state == TaskState.task_state_working


async def test_update_task_terminal_guard(storage):
    """Updating a task in a terminal state raises TaskTerminalStateError."""
    task = await storage.create_task("ctx-1", _msg())
    await storage.update_task(task.id, state=TaskState.task_state_completed)

    with pytest.raises(TaskTerminalStateError):
        await storage.update_task(task.id, state=TaskState.task_state_working)


async def test_update_task_occ(storage):
    """Passing a wrong expected_version raises ConcurrencyError."""
    task = await storage.create_task("ctx-1", _msg())

    with pytest.raises(ConcurrencyError):
        await storage.update_task(
            task.id, state=TaskState.task_state_working, expected_version=999
        )


async def test_history_length_trimming(storage):
    """Loading with historyLength=1 returns only the last message."""
    task = await storage.create_task("ctx-1", _msg("first", "m1"))
    second = _msg("second", "m2")
    second = second.model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
    await storage.update_task(task.id, messages=[second])

    loaded = await storage.load_task(task.id, history_length=1)
    assert loaded is not None
    assert len(loaded.history) == 1
    # The last message should be the second one
    text_part = loaded.history[0].parts[0]
    assert text_part.text == "second"


async def test_delete_task(storage):
    """Deleting a task makes it no longer loadable."""
    task = await storage.create_task("ctx-1", _msg())
    deleted = await storage.delete_task(task.id)

    assert deleted is True
    assert await storage.load_task(task.id) is None


async def test_delete_context(storage):
    """Deleting a context removes all tasks in that context."""
    t1 = await storage.create_task("ctx-shared", _msg("a", "m1"))
    t2 = await storage.create_task("ctx-shared", _msg("b", "m2"))

    count = await storage.delete_context("ctx-shared")
    assert count == 2
    assert await storage.load_task(t1.id) is None
    assert await storage.load_task(t2.id) is None


async def test_delete_task_cascades_to_push_store(storage):
    """delete_task MUST cascade to PushConfigStore.delete_configs_for_task
    so push configs don't orphan in the DB."""
    from a2akit.push.models import PushNotificationConfig
    from a2akit.push.store import InMemoryPushConfigStore

    push_store = InMemoryPushConfigStore()
    storage.bind_push_store(push_store)

    task = await storage.create_task("ctx-1", _msg())
    await push_store.set_config(
        task.id,
        PushNotificationConfig(id="cfg-1", url="https://example.com/hook"),
    )
    assert len(await push_store.list_configs(task.id)) == 1

    assert await storage.delete_task(task.id) is True
    assert await push_store.list_configs(task.id) == []


async def test_delete_context_cascades_to_push_store(storage):
    """delete_context MUST cascade push-config cleanup for every deleted task."""
    from a2akit.push.models import PushNotificationConfig
    from a2akit.push.store import InMemoryPushConfigStore

    push_store = InMemoryPushConfigStore()
    storage.bind_push_store(push_store)

    t1 = await storage.create_task("ctx-cascade", _msg("a", "m1"))
    t2 = await storage.create_task("ctx-cascade", _msg("b", "m2"))
    await push_store.set_config(
        t1.id, PushNotificationConfig(id="c1", url="https://example.com/1")
    )
    await push_store.set_config(
        t2.id, PushNotificationConfig(id="c2", url="https://example.com/2")
    )

    count = await storage.delete_context("ctx-cascade")
    assert count == 2
    assert await push_store.list_configs(t1.id) == []
    assert await push_store.list_configs(t2.id) == []


async def test_delete_task_cascade_failure_does_not_break_delete(storage):
    """If the push store errors during cascade, the primary delete still succeeds."""
    from a2akit.push.store import InMemoryPushConfigStore

    class BrokenStore(InMemoryPushConfigStore):
        async def delete_configs_for_task(self, task_id):  # type: ignore[override]
            raise RuntimeError("push store is down")

    storage.bind_push_store(BrokenStore())

    task = await storage.create_task("ctx-resilient", _msg())
    # Must not raise despite the cascade failure.
    assert await storage.delete_task(task.id) is True
    assert await storage.load_task(task.id) is None


async def test_idempotency(storage):
    """Creating two tasks with the same idempotency key and context returns the same task."""
    t1 = await storage.create_task("ctx-1", _msg("a", "m1"), idempotency_key="idem-1")
    t2 = await storage.create_task("ctx-1", _msg("b", "m2"), idempotency_key="idem-1")

    assert t1.id == t2.id


async def test_list_tasks_status_timestamp_after(storage):
    """statusTimestampAfter returns only tasks whose status changed after the cutoff."""
    t1 = await storage.create_task("ctx-1", _msg("a", "m1"))
    cutoff = _ts(await storage.load_task(t1.id))
    await anyio.sleep(0.01)  # ensure a strictly later timestamp
    t2 = await storage.create_task("ctx-1", _msg("b", "m2"))

    result = await storage.list_tasks(ListTasksQuery(status_timestamp_after=cutoff))
    assert [t.id for t in result.tasks] == [t2.id]
    assert result.total_size == 1


async def test_list_tasks_tenant_filter(storage):
    """tenant filter matches only tasks whose metadata carries the tenant."""
    t1 = await storage.create_task("ctx-1", _msg("a", "m1"))
    await storage.update_task(t1.id, task_metadata={META_TENANT_KEY: "acme"})
    await storage.create_task("ctx-1", _msg("b", "m2"))

    result = await storage.list_tasks(ListTasksQuery(tenant="acme"))
    assert [t.id for t in result.tasks] == [t1.id]
    assert result.total_size == 1


async def test_update_task_does_not_store_input_references(storage):
    """Mutating a message after update_task must not change stored state."""
    from a2a_pydantic.v10 import Message as V10Message
    from a2a_pydantic.v10 import Part as V10Part
    from a2a_pydantic.v10 import Role as V10Role

    task = await storage.create_task("ctx-1", _msg("first", "m1"))
    m2 = V10Message(
        role=V10Role.role_user,
        parts=[V10Part(text="second")],
        message_id="m2",
        task_id=task.id,
        context_id="ctx-1",
    )
    await storage.update_task(task.id, messages=[m2])

    m2.parts[0].text = "mutated"

    loaded = await storage.load_task(task.id)
    assert loaded is not None
    assert loaded.history[1].parts[0].text == "second"


async def test_update_task_atomic_on_partial_failure(storage):
    """A failure during artifact application must leave the task untouched."""
    task = await storage.create_task("ctx-1", _msg("first", "m1"))

    def boom(*args, **kwargs):
        raise RuntimeError("artifact application failed")

    storage._apply_artifact = boom  # instance attribute shadows the method

    m2 = _msg("second", "m2").model_copy(update={"task_id": task.id, "context_id": "ctx-1"})
    art = Artifact(artifact_id="a1", parts=[Part(root=TextPart(text="x"))])
    with pytest.raises(RuntimeError):
        await storage.update_task(
            task.id,
            state=TaskState.task_state_working,
            messages=[m2],
            artifacts=[ArtifactWrite(artifact=art)],
        )

    loaded = await storage.load_task(task.id)
    assert loaded is not None
    # Neither the message append nor the state change may be visible.
    assert len(loaded.history) == 1
    assert loaded.status.state == TaskState.task_state_submitted
    assert await storage.get_version(task.id) == 1
