"""Unit tests for InMemoryStorage CRUD operations."""

from __future__ import annotations

import pytest
from a2a.types import Message, Part, Role, TaskState, TextPart

from a2akit.storage.base import ConcurrencyError, TaskTerminalStateError


def _msg(text: str = "hello", msg_id: str = "msg1") -> Message:
    """Create a simple user message."""
    return Message(
        role=Role.user,
        parts=[Part(root=TextPart(text=text))],
        message_id=msg_id,
    )


async def test_create_task(storage):
    """Creating a task returns a Task with id, contextId, and submitted state."""
    task = await storage.create_task("ctx-1", _msg())

    assert task.id, "Task must have a non-empty id"
    assert task.context_id == "ctx-1"
    assert task.status.state == TaskState.submitted


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
    await storage.update_task(task.id, state=TaskState.working)

    loaded = await storage.load_task(task.id)
    assert loaded is not None
    assert loaded.status.state == TaskState.working


async def test_update_task_terminal_guard(storage):
    """Updating a task in a terminal state raises TaskTerminalStateError."""
    task = await storage.create_task("ctx-1", _msg())
    await storage.update_task(task.id, state=TaskState.completed)

    with pytest.raises(TaskTerminalStateError):
        await storage.update_task(task.id, state=TaskState.working)


async def test_update_task_occ(storage):
    """Passing a wrong expected_version raises ConcurrencyError."""
    task = await storage.create_task("ctx-1", _msg())

    with pytest.raises(ConcurrencyError):
        await storage.update_task(task.id, state=TaskState.working, expected_version=999)


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
    text_part = loaded.history[0].parts[0].root
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


async def test_idempotency(storage):
    """Creating two tasks with the same idempotency key and context returns the same task."""
    t1 = await storage.create_task("ctx-1", _msg("a", "m1"), idempotency_key="idem-1")
    t2 = await storage.create_task("ctx-1", _msg("b", "m2"), idempotency_key="idem-1")

    assert t1.id == t2.id
