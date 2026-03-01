"""Tests for worker/base.py — _build_parts edge cases, _extract_files,
_extract_data_parts, _versioned_update OCC retry, request_auth, emit_data_artifact."""

from __future__ import annotations

import base64
import uuid

import anyio
import httpx
import pytest
from a2a.types import (
    DataPart,
    FilePart,
    FileWithBytes,
    FileWithUri,
    Message,
    Part,
    Role,
    TaskState,
    TextPart,
)
from asgi_lifespan import LifespanManager

from a2akit.broker.memory import AnyioCancelScope
from a2akit.event_bus.memory import InMemoryEventBus
from a2akit.event_emitter import DefaultEventEmitter
from a2akit.storage.base import TaskTerminalStateError
from a2akit.storage.memory import InMemoryStorage
from a2akit.worker.base import (
    TaskContextImpl,
    _build_parts,
    _extract_data_parts,
    _extract_files,
)
from conftest import _make_app


def test_build_parts_text():
    """_build_parts with text only."""
    parts = _build_parts(text="hello")
    assert len(parts) == 1
    assert isinstance(parts[0].root, TextPart)
    assert parts[0].root.text == "hello"


def test_build_parts_data():
    """_build_parts with data only."""
    parts = _build_parts(data={"key": "value"})
    assert len(parts) == 1
    assert isinstance(parts[0].root, DataPart)
    assert parts[0].root.data == {"key": "value"}


def test_build_parts_file_bytes():
    """_build_parts with file_bytes."""
    parts = _build_parts(
        file_bytes=b"binary data", media_type="application/pdf", filename="doc.pdf"
    )
    assert len(parts) == 1
    root = parts[0].root
    assert isinstance(root, FilePart)
    assert isinstance(root.file, FileWithBytes)
    assert root.file.name == "doc.pdf"
    assert root.file.mime_type == "application/pdf"
    # Check that bytes are base64 encoded
    decoded = base64.b64decode(root.file.bytes)
    assert decoded == b"binary data"


def test_build_parts_file_url():
    """_build_parts with file_url."""
    parts = _build_parts(file_url="https://example.com/file.pdf", media_type="application/pdf")
    assert len(parts) == 1
    root = parts[0].root
    assert isinstance(root, FilePart)
    assert isinstance(root.file, FileWithUri)
    assert root.file.uri == "https://example.com/file.pdf"


def test_build_parts_multiple():
    """_build_parts with text + data."""
    parts = _build_parts(text="hello", data={"key": "value"})
    assert len(parts) == 2


def test_build_parts_empty_raises():
    """_build_parts with no args raises ValueError."""
    with pytest.raises(ValueError, match="At least one content parameter"):
        _build_parts()


def test_build_parts_text_and_file():
    """_build_parts with text + file_bytes produces two parts."""
    parts = _build_parts(text="hello", file_bytes=b"data", media_type="application/octet-stream")
    assert len(parts) == 2
    assert isinstance(parts[0].root, TextPart)
    assert isinstance(parts[1].root, FilePart)


def test_extract_files_with_bytes():
    """_extract_files extracts FileInfo from FileWithBytes parts."""
    content = b"test content"
    encoded = base64.b64encode(content).decode("ascii")
    file_part = Part(
        FilePart(file=FileWithBytes(bytes=encoded, mime_type="text/plain", name="test.txt"))
    )
    files = _extract_files([file_part])
    assert len(files) == 1
    assert files[0].content == content
    assert files[0].filename == "test.txt"
    assert files[0].media_type == "text/plain"
    assert files[0].url is None


def test_extract_files_with_uri():
    """_extract_files extracts FileInfo from FileWithUri parts."""
    file_part = Part(
        FilePart(file=FileWithUri(uri="https://example.com/test.pdf", mime_type="application/pdf"))
    )
    files = _extract_files([file_part])
    assert len(files) == 1
    assert files[0].url == "https://example.com/test.pdf"
    assert files[0].content is None
    assert files[0].media_type == "application/pdf"


def test_extract_files_skips_non_file_parts():
    """_extract_files skips text and data parts."""
    parts = [
        Part(TextPart(text="hello")),
        Part(DataPart(data={"key": "val"})),
    ]
    files = _extract_files(parts)
    assert len(files) == 0


def test_extract_data_parts_with_dict():
    """_extract_data_parts extracts dicts from DataPart."""
    parts = [
        Part(TextPart(text="hello")),
        Part(DataPart(data={"key": "value"})),
        Part(DataPart(data={"another": "dict"})),
    ]
    result = _extract_data_parts(parts)
    assert len(result) == 2
    assert result[0] == {"key": "value"}
    assert result[1] == {"another": "dict"}


def test_extract_data_parts_skips_text():
    """_extract_data_parts skips TextPart."""
    parts = [
        Part(TextPart(text="just text")),
    ]
    result = _extract_data_parts(parts)
    assert len(result) == 0


async def _make_ctx(storage=None, event_bus=None, state=TaskState.working):
    """Helper to create a TaskContextImpl with real storage/event_bus."""
    if storage is None:
        storage = InMemoryStorage()
    if event_bus is None:
        event_bus = InMemoryEventBus()
        await event_bus.__aenter__()

    emitter = DefaultEventEmitter(event_bus, storage)
    msg = Message(
        role=Role.user,
        parts=[Part(TextPart(text="hello"))],
        message_id=str(uuid.uuid4()),
    )
    task = await storage.create_task("ctx-1", msg)
    if state != TaskState.submitted:
        version = await storage.update_task(task.id, state=state)
    else:
        version = await storage.get_version(task.id)

    cancel_event = anyio.Event()
    cancel_scope = AnyioCancelScope(cancel_event)

    ctx = TaskContextImpl(
        task_id=task.id,
        context_id="ctx-1",
        message_id=msg.message_id,
        user_text="hello",
        parts=msg.parts,
        metadata={},
        emitter=emitter,
        cancel_event=cancel_scope,
        storage=storage,
        initial_version=version,
    )
    return ctx, storage, event_bus, task


async def test_ctx_files_property():
    """TaskContextImpl.files returns FileInfo list from parts."""
    storage = InMemoryStorage()
    async with InMemoryEventBus() as event_bus:
        emitter = DefaultEventEmitter(event_bus, storage)
        msg = Message(
            role=Role.user,
            parts=[
                Part(TextPart(text="hello")),
                Part(FilePart(file=FileWithUri(uri="https://example.com/file.pdf"))),
            ],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        cancel_scope = AnyioCancelScope(anyio.Event())

        ctx = TaskContextImpl(
            task_id=task.id,
            context_id="ctx-1",
            message_id=msg.message_id,
            user_text="hello",
            parts=msg.parts,
            metadata={},
            emitter=emitter,
            cancel_event=cancel_scope,
            storage=storage,
        )
        files = ctx.files
        assert len(files) == 1
        assert files[0].url == "https://example.com/file.pdf"


async def test_ctx_data_parts_property():
    """TaskContextImpl.data_parts returns data dicts from parts."""
    storage = InMemoryStorage()
    async with InMemoryEventBus() as event_bus:
        emitter = DefaultEventEmitter(event_bus, storage)
        msg = Message(
            role=Role.user,
            parts=[
                Part(TextPart(text="hello")),
                Part(DataPart(data={"key": "value"})),
            ],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        cancel_scope = AnyioCancelScope(anyio.Event())

        ctx = TaskContextImpl(
            task_id=task.id,
            context_id="ctx-1",
            message_id=msg.message_id,
            user_text="hello",
            parts=msg.parts,
            metadata={},
            emitter=emitter,
            cancel_event=cancel_scope,
            storage=storage,
        )
        data = ctx.data_parts
        assert len(data) == 1
        assert data[0] == {"key": "value"}


async def test_ctx_is_cancelled():
    """TaskContextImpl.is_cancelled reflects cancel event state."""
    storage = InMemoryStorage()
    async with InMemoryEventBus() as event_bus:
        emitter = DefaultEventEmitter(event_bus, storage)
        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        cancel_ev = anyio.Event()
        cancel_scope = AnyioCancelScope(cancel_ev)

        ctx = TaskContextImpl(
            task_id=task.id,
            context_id="ctx-1",
            message_id=msg.message_id,
            user_text="hello",
            emitter=emitter,
            cancel_event=cancel_scope,
            storage=storage,
        )
        assert ctx.is_cancelled is False
        cancel_ev.set()
        assert ctx.is_cancelled is True


async def test_ctx_previous_artifacts():
    """TaskContextImpl.previous_artifacts returns the artifacts list."""
    from a2akit.worker.base import PreviousArtifact

    storage = InMemoryStorage()
    async with InMemoryEventBus() as event_bus:
        emitter = DefaultEventEmitter(event_bus, storage)
        msg = Message(
            role=Role.user,
            parts=[Part(TextPart(text="hello"))],
            message_id=str(uuid.uuid4()),
        )
        task = await storage.create_task("ctx-1", msg)
        cancel_scope = AnyioCancelScope(anyio.Event())

        prev = [PreviousArtifact(artifact_id="a1", name="test", parts=[])]
        ctx = TaskContextImpl(
            task_id=task.id,
            context_id="ctx-1",
            message_id=msg.message_id,
            user_text="hello",
            emitter=emitter,
            cancel_event=cancel_scope,
            storage=storage,
            previous_artifacts=prev,
        )
        assert ctx.previous_artifacts == prev


async def test_versioned_update_concurrency_retry_non_terminal():
    """_versioned_update retries once with fresh version on ConcurrencyError for non-terminal task."""
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        # Bump the version externally to cause mismatch
        await storage.update_task(task.id, state=TaskState.working)

        # Now ctx._version is stale, but task is non-terminal so it should retry
        await ctx._versioned_update(task.id, state=TaskState.working)
        # Should succeed after retry
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_versioned_update_concurrency_terminal():
    """_versioned_update raises TaskTerminalStateError when task becomes terminal during retry."""
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        # Complete the task and bump version
        await storage.update_task(task.id, state=TaskState.completed)

        # ctx._version is stale and task is terminal
        with pytest.raises(TaskTerminalStateError):
            await ctx._versioned_update(task.id, state=TaskState.working)
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_emit_data_artifact():
    """emit_data_artifact emits a data artifact with correct structure."""
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        await ctx.emit_data_artifact({"result": "ok"}, artifact_id="data-1")

        loaded = await storage.load_task(task.id)
        assert len(loaded.artifacts) == 1
        art = loaded.artifacts[0]
        assert art.artifact_id == "data-1"
        # Check that the data part is present
        data_parts = [p for p in art.parts if isinstance(p.root, DataPart)]
        assert len(data_parts) == 1
        assert data_parts[0].root.data == {"result": "ok"}
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_request_auth_via_http():
    """AuthRequiredWorker transitions to auth-required via request_auth."""
    from conftest import AuthRequiredWorker

    app = _make_app(AuthRequiredWorker())
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            body = {
                "message": {
                    "role": "user",
                    "messageId": str(uuid.uuid4()),
                    "parts": [{"kind": "text", "text": "do stuff"}],
                },
                "configuration": {"blocking": True},
            }
            resp = await client.post("/v1/message:send", json=body)
            assert resp.status_code == 200
            task = resp.json()
            assert task["status"]["state"] == "auth-required"


async def test_respond_no_text():
    """respond() with no text still completes the task."""
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        await ctx.respond()
        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.completed
        assert ctx.turn_ended is True
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_load_context_no_context_id():
    """load_context returns None when context_id is None."""
    ctx, _storage, event_bus, _task = await _make_ctx()
    ctx.context_id = None
    try:
        result = await ctx.load_context()
        assert result is None
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_update_context_no_context_id():
    """update_context is a no-op when context_id is None."""
    ctx, _storage, event_bus, _task = await _make_ctx()
    ctx.context_id = None
    try:
        await ctx.update_context({"data": "test"})
        # Should not raise, just no-op
    finally:
        await event_bus.__aexit__(None, None, None)
