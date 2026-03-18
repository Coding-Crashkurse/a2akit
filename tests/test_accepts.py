"""Tests for ctx.accepts() — output mode negotiation."""

from __future__ import annotations

import uuid

import anyio
from a2a.types import Message, Part, Role, TextPart

from a2akit.broker.memory import AnyioCancelScope
from a2akit.event_bus.memory import InMemoryEventBus
from a2akit.event_emitter import DefaultEventEmitter
from a2akit.storage.memory import InMemoryStorage
from a2akit.worker.base import TaskContextImpl


async def _make_ctx(accepted_output_modes: list[str] | None = None) -> TaskContextImpl:
    """Build a minimal TaskContextImpl with the given accepted_output_modes."""
    storage = InMemoryStorage()
    event_bus = InMemoryEventBus()
    await event_bus.__aenter__()
    emitter = DefaultEventEmitter(event_bus, storage)
    msg = Message(
        role=Role.user,
        parts=[Part(TextPart(text="hello"))],
        message_id=str(uuid.uuid4()),
    )
    task = await storage.create_task("ctx-1", msg)
    cancel_scope = AnyioCancelScope(anyio.Event())

    return TaskContextImpl(
        task_id=task.id,
        context_id="ctx-1",
        message_id=msg.message_id or "",
        user_text="hello",
        parts=msg.parts,
        metadata={},
        emitter=emitter,
        cancel_event=cancel_scope,
        storage=storage,
        accepted_output_modes=accepted_output_modes,
    )


async def test_accepts_when_client_specifies_modes():
    """accepts() returns True for listed types, False for others."""
    ctx = await _make_ctx(["application/json", "text/plain"])
    assert ctx.accepts("application/json") is True
    assert ctx.accepts("text/plain") is True
    assert ctx.accepts("image/png") is False


async def test_accepts_when_no_filter():
    """accepts() returns True for everything when no modes specified."""
    ctx = await _make_ctx(None)
    assert ctx.accepts("application/json") is True
    assert ctx.accepts("image/png") is True
    assert ctx.accepts("anything/whatever") is True


async def test_accepts_when_empty_list():
    """accepts() returns True for everything when empty list."""
    ctx = await _make_ctx([])
    assert ctx.accepts("application/json") is True
    assert ctx.accepts("image/png") is True


async def test_accepts_case_sensitive():
    """accepts() is case-sensitive per RFC 2045."""
    ctx = await _make_ctx(["application/json"])
    assert ctx.accepts("application/json") is True
    assert ctx.accepts("Application/JSON") is False
