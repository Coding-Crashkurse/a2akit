"""Tests for task_manager.py — covers subscribe_task, stream_message, blocking send,
force_cancel_after, _find_direct_reply, _is_agent_role, and edge cases."""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import httpx
import pytest
from a2a.types import (
    Message,
    MessageSendParams,
    Part,
    Role,
    Task,
    TaskState,
    TaskStatus,
    TextPart,
)
from asgi_lifespan import LifespanManager

from a2akit import (
    InMemoryBroker,
    InMemoryEventBus,
    InMemoryStorage,
)
from a2akit.broker.memory import InMemoryCancelRegistry
from a2akit.storage.base import (
    ContextMismatchError,
    TaskNotAcceptingMessagesError,
    TaskNotCancelableError,
    TaskNotFoundError,
    TaskTerminalStateError,
    UnsupportedOperationError,
)
from a2akit.task_manager import TaskManager, _find_direct_reply, _is_agent_role
from conftest import EchoWorker, _make_app


def test_is_agent_role_with_string():
    assert _is_agent_role("agent") is True


def test_is_agent_role_with_enum():
    assert _is_agent_role(Role.agent) is True


def test_is_agent_role_with_user():
    assert _is_agent_role(Role.user) is False
    assert _is_agent_role("user") is False


def test_is_agent_role_with_none():
    assert _is_agent_role(None) is False


def test_find_direct_reply_no_metadata():
    """Returns None when task has no metadata."""
    task = Task(
        id="t1",
        context_id="c1",
        kind="task",
        status=TaskStatus(state=TaskState.completed, timestamp="2024-01-01"),
        history=[],
    )
    assert _find_direct_reply(task) is None


def test_find_direct_reply_no_history():
    """Returns None when task has metadata marker but no history."""
    task = Task(
        id="t1",
        context_id="c1",
        kind="task",
        status=TaskStatus(state=TaskState.completed, timestamp="2024-01-01"),
        history=[],
        metadata={"_a2akit_direct_reply": "msg-123"},
    )
    assert _find_direct_reply(task) is None


def test_find_direct_reply_with_matching_message():
    """Returns the message when metadata key matches a history message."""
    msg = Message(
        role=Role.agent,
        parts=[Part(TextPart(text="direct reply"))],
        message_id="msg-123",
    )
    task = Task(
        id="t1",
        context_id="c1",
        kind="task",
        status=TaskStatus(state=TaskState.completed, timestamp="2024-01-01"),
        history=[msg],
        metadata={"_a2akit_direct_reply": "msg-123"},
    )
    result = _find_direct_reply(task)
    assert result is not None
    assert result.message_id == "msg-123"


def test_find_direct_reply_no_match():
    """Returns None when metadata key doesn't match any history message."""
    msg = Message(
        role=Role.agent,
        parts=[Part(TextPart(text="other"))],
        message_id="msg-999",
    )
    task = Task(
        id="t1",
        context_id="c1",
        kind="task",
        status=TaskStatus(state=TaskState.completed, timestamp="2024-01-01"),
        history=[msg],
        metadata={"_a2akit_direct_reply": "msg-not-found"},
    )
    assert _find_direct_reply(task) is None


async def test_subscribe_task_not_found():
    """subscribe_task raises TaskNotFoundError for non-existent task."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
        )
        with pytest.raises(TaskNotFoundError):
            async for _ in tm.subscribe_task("nonexistent"):
                pass


async def test_subscribe_task_terminal_state():
    """subscribe_task raises UnsupportedOperationError for terminal-state task."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
        )
        # Create and complete a task
        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        await storage.update_task(task.id, state=TaskState.completed)

        with pytest.raises(UnsupportedOperationError):
            async for _ in tm.subscribe_task(task.id):
                pass


async def test_cancel_task_not_found():
    """cancel_task raises TaskNotFoundError for non-existent task."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
        )
        with pytest.raises(TaskNotFoundError):
            await tm.cancel_task("nonexistent")


async def test_cancel_task_terminal():
    """cancel_task raises TaskNotCancelableError for completed task."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
        )
        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hi"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        await storage.update_task(task.id, state=TaskState.completed)

        with pytest.raises(TaskNotCancelableError):
            await tm.cancel_task(task.id)


async def test_cancel_task_success():
    """cancel_task registers cancel and returns current task state."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
        )
        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hi"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        await storage.update_task(task.id, state=TaskState.working)

        result = await tm.cancel_task(task.id)
        assert result is not None
        assert result.id == task.id

        # Cancel was registered
        assert await cancel_reg.is_cancelled(task.id)


async def test_force_cancel_after_non_terminal():
    """_force_cancel_after force-cancels a task stuck in non-terminal state."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
        )
        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hi"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        await storage.update_task(task.id, state=TaskState.working)

        # Use a very short deadline so it completes quickly
        await tm._force_cancel_after(task.id, 0.0)

        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.canceled


async def test_force_cancel_after_already_terminal():
    """_force_cancel_after does nothing if task is already terminal."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
        )
        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hi"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        await storage.update_task(task.id, state=TaskState.completed)

        await tm._force_cancel_after(task.id, 0.0)

        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.completed


async def test_force_cancel_after_task_not_found():
    """_force_cancel_after returns gracefully if task doesn't exist."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
        )
        # Should not raise
        await tm._force_cancel_after("nonexistent", 0.0)


async def test_force_cancel_after_exception_handling():
    """_force_cancel_after logs exception but doesn't raise."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
        )
        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hi"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        await storage.update_task(task.id, state=TaskState.working)

        # Break storage to trigger exception

        async def broken_load(task_id, *args, **kwargs):
            raise RuntimeError("Storage broken")

        storage.load_task = broken_load

        # Should not raise - exception is caught and logged
        await tm._force_cancel_after(task.id, 0.0)


async def test_send_message_follow_up_to_terminal():
    """Sending a follow-up message to a terminal task raises TaskTerminalStateError."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
        )
        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hi"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        await storage.update_task(task.id, state=TaskState.completed)

        follow_up = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(TextPart(text="follow up"))],
                message_id=str(uuid.uuid4()),
                task_id=task.id,
            ),
            configuration={"blocking": True},
        )
        with pytest.raises(TaskTerminalStateError):
            await tm.send_message(follow_up)


async def test_send_message_follow_up_to_nonexistent():
    """Sending a follow-up message to a nonexistent task raises TaskNotFoundError."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
        )
        follow_up = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(TextPart(text="follow up"))],
                message_id=str(uuid.uuid4()),
                task_id="nonexistent",
            ),
            configuration={"blocking": True},
        )
        with pytest.raises(TaskNotFoundError):
            await tm.send_message(follow_up)


async def test_send_message_context_mismatch():
    """Sending a follow-up with wrong contextId raises ContextMismatchError."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
        )
        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hi"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        await storage.update_task(task.id, state=TaskState.input_required)

        follow_up = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(TextPart(text="follow up"))],
                message_id=str(uuid.uuid4()),
                task_id=task.id,
                context_id="wrong-context",
            ),
            configuration={"blocking": True},
        )
        with pytest.raises(ContextMismatchError):
            await tm.send_message(follow_up)


async def test_send_message_not_accepting():
    """Sending a user message to a working task raises TaskNotAcceptingMessagesError."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
        )
        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hi"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        await storage.update_task(task.id, state=TaskState.working)

        follow_up = MessageSendParams(
            message=Message(
                role=Role.user,
                parts=[Part(TextPart(text="follow up"))],
                message_id=str(uuid.uuid4()),
                task_id=task.id,
            ),
            configuration={"blocking": True},
        )
        with pytest.raises(TaskNotAcceptingMessagesError):
            await tm.send_message(follow_up)


async def test_on_background_done_logs_exception():
    """_on_background_done handles exceptions from failed background tasks."""
    storage = InMemoryStorage()
    async with InMemoryBroker() as broker, InMemoryEventBus() as event_bus:
        cancel_reg = InMemoryCancelRegistry()
        tm = TaskManager(
            broker=broker,
            storage=storage,
            event_bus=event_bus,
            cancel_registry=cancel_reg,
        )

        async def failing_coro():
            raise RuntimeError("background failure")

        task = asyncio.create_task(failing_coro())
        tm._background_tasks.add(task)
        with contextlib.suppress(RuntimeError):
            await task

        # Call the callback manually
        tm._on_background_done(task)
        # Task should be discarded from background set
        assert task not in tm._background_tasks


async def test_stream_message_with_history_length():
    """stream_message respects history_length configuration."""
    app = _make_app(EchoWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "hello"}],
                },
                "configuration": {"historyLength": 0},
            }
            raw_text = ""
            async with client.stream("POST", "/v1/message:stream", json=body) as resp:
                assert resp.status_code == 200
                async for chunk in resp.aiter_text():
                    raw_text += chunk
            assert len(raw_text) > 0
