"""Tests for worker/base.py — _build_parts edge cases, extract_files,
extract_data, _versioned_update OCC retry, request_auth, emit_data_artifact."""

from __future__ import annotations

import base64
import uuid

import anyio
import httpx
import pytest
from a2a.types import (
    DataPart,
    FilePart,
    FileWithUri,
    Message,
    Part,
    Role,
    TextPart,
)
from a2a_pydantic.v10 import Part as V10Part
from a2a_pydantic.v10 import TaskState
from asgi_lifespan import LifespanManager

from a2akit._parts import extract_data, extract_files
from a2akit.broker.memory import AnyioCancelScope
from a2akit.event_bus.memory import InMemoryEventBus
from a2akit.event_emitter import DefaultEventEmitter
from a2akit.storage.base import TaskTerminalStateError
from a2akit.storage.memory import InMemoryStorage
from a2akit.worker.base import (
    TaskContextImpl,
    _build_parts,
)
from conftest import _make_app


def test_build_parts_text():
    """_build_parts with text only."""
    parts = _build_parts(text="hello")
    assert len(parts) == 1
    assert parts[0].text == "hello"


def test_build_parts_data():
    """_build_parts with data only."""
    parts = _build_parts(data={"key": "value"})
    assert len(parts) == 1
    assert parts[0].data.root == {"key": "value"}


def test_build_parts_file_bytes():
    """_build_parts with file_bytes."""
    parts = _build_parts(
        file_bytes=b"binary data", media_type="application/pdf", filename="doc.pdf"
    )
    assert len(parts) == 1
    p = parts[0]
    assert p.filename == "doc.pdf"
    assert p.media_type == "application/pdf"
    # Check that bytes are base64 encoded
    decoded = base64.b64decode(p.raw)
    assert decoded == b"binary data"


def test_build_parts_file_url():
    """_build_parts with file_url."""
    parts = _build_parts(file_url="https://example.com/file.pdf", media_type="application/pdf")
    assert len(parts) == 1
    p = parts[0]
    assert p.url == "https://example.com/file.pdf"


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
    assert parts[0].text == "hello"
    assert parts[1].raw is not None


def test_extract_files_with_bytes():
    """extract_files extracts FileInfo from FileWithBytes parts."""
    content = b"test content"
    file_part = V10Part(raw=content, media_type="text/plain", filename="test.txt")
    files = extract_files([file_part])
    assert len(files) == 1
    assert files[0].content == content
    assert files[0].filename == "test.txt"
    assert files[0].media_type == "text/plain"
    assert files[0].url is None


def test_extract_files_with_uri():
    """extract_files extracts FileInfo from FileWithUri parts."""
    file_part = V10Part(url="https://example.com/test.pdf", media_type="application/pdf")
    files = extract_files([file_part])
    assert len(files) == 1
    assert files[0].url == "https://example.com/test.pdf"
    assert files[0].content is None
    assert files[0].media_type == "application/pdf"


def test_extract_files_skips_non_file_parts():
    """extract_files skips text and data parts."""
    parts = [
        V10Part(text="hello"),
        V10Part(data={"key": "val"}),
    ]
    files = extract_files(parts)
    assert len(files) == 0


def test_extract_data_with_dict():
    """extract_data extracts dicts from DataPart."""
    parts = [
        V10Part(text="hello"),
        V10Part(data={"key": "value"}),
        V10Part(data={"another": "dict"}),
    ]
    result = extract_data(parts)
    assert len(result) == 2
    assert result[0] == {"key": "value"}
    assert result[1] == {"another": "dict"}


def test_extract_data_skips_text():
    """extract_data skips TextPart."""
    parts = [
        V10Part(text="just text"),
    ]
    result = extract_data(parts)
    assert len(result) == 0


async def _make_ctx(storage=None, event_bus=None, state=TaskState.task_state_working):
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
    if state != TaskState.task_state_submitted:
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

        v10_parts = [
            V10Part(text="hello"),
            V10Part(url="https://example.com/file.pdf"),
        ]
        ctx = TaskContextImpl(
            task_id=task.id,
            context_id="ctx-1",
            message_id=msg.message_id,
            user_text="hello",
            parts=v10_parts,
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

        v10_parts = [
            V10Part(text="hello"),
            V10Part(data={"key": "value"}),
        ]
        ctx = TaskContextImpl(
            task_id=task.id,
            context_id="ctx-1",
            message_id=msg.message_id,
            user_text="hello",
            parts=v10_parts,
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
        await storage.update_task(task.id, state=TaskState.task_state_working)

        # Now ctx._version is stale, but task is non-terminal so it should retry
        await ctx._versioned_update(task.id, state=TaskState.task_state_working)
        # Should succeed after retry
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_versioned_update_concurrency_terminal():
    """_versioned_update raises TaskTerminalStateError when task becomes terminal during retry."""
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        # Complete the task and bump version
        await storage.update_task(task.id, state=TaskState.task_state_completed)

        # ctx._version is stale and task is terminal
        with pytest.raises(TaskTerminalStateError):
            await ctx._versioned_update(task.id, state=TaskState.task_state_working)
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_emit_data_artifact():
    """emit_data_artifact emits a data artifact with correct structure.

    Artifact is buffered and written to DB on complete().
    """
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        await ctx.emit_data_artifact({"result": "ok"}, artifact_id="data-1")

        # Buffered — not yet in DB
        assert len(ctx._pending_artifacts) == 1

        # complete() drains buffer into terminal write
        await ctx.complete()

        loaded = await storage.load_task(task.id)
        assert len(loaded.artifacts) == 1
        art = loaded.artifacts[0]
        assert art.artifact_id == "data-1"
        data_parts = [p for p in art.parts if p.data is not None]
        assert len(data_parts) == 1
        assert data_parts[0].data.root == {"result": "ok"}
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
        assert loaded.status.state == TaskState.task_state_completed
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


async def test_artifacts_buffered_not_written_immediately():
    """Chunks within flush interval stay in buffer, no DB write."""
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        # Set high thresholds so nothing auto-flushes
        ctx._flush_interval = 999
        ctx._flush_count = 999
        ctx._last_flush = __import__("time").monotonic()

        for i in range(5):
            await ctx.emit_text_artifact(f"chunk {i}", artifact_id="stream", append=True)

        # All 5 in buffer, 0 in DB
        assert len(ctx._pending_artifacts) == 5
        loaded = await storage.load_task(task.id)
        assert len(loaded.artifacts) == 0
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_buffer_flushes_on_count_threshold():
    """Buffer auto-flushes when chunk count exceeds _flush_count."""
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        ctx._flush_count = 3
        ctx._flush_interval = 999  # disable time-based flush
        ctx._last_flush = __import__("time").monotonic()

        await ctx.emit_text_artifact("c1", artifact_id="s", append=True)
        await ctx.emit_text_artifact("c2", artifact_id="s", append=True)
        assert len(ctx._pending_artifacts) == 2  # below threshold

        await ctx.emit_text_artifact("c3", artifact_id="s", append=True)
        # Hit threshold → flushed
        assert len(ctx._pending_artifacts) == 0

        loaded = await storage.load_task(task.id)
        assert len(loaded.artifacts) >= 1
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_buffer_flushes_on_time_threshold():
    """Buffer auto-flushes when time since last flush exceeds interval."""
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        ctx._flush_count = 999  # disable count-based flush
        ctx._flush_interval = 0.0  # always flush on time
        ctx._last_flush = 0.0  # force elapsed > interval

        await ctx.emit_text_artifact("chunk", artifact_id="s", append=True)
        # Time threshold exceeded → flushed immediately
        assert len(ctx._pending_artifacts) == 0

        loaded = await storage.load_task(task.id)
        assert len(loaded.artifacts) >= 1
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_complete_drains_buffer():
    """complete() writes all pending chunks + final artifact in 1 DB call."""
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        ctx._flush_interval = 999
        ctx._flush_count = 999
        ctx._last_flush = __import__("time").monotonic()

        await ctx.emit_text_artifact("chunk 1", artifact_id="stream", append=True)
        await ctx.emit_text_artifact("chunk 2", artifact_id="stream", append=True)
        assert len(ctx._pending_artifacts) == 2

        await ctx.complete("Done")

        assert len(ctx._pending_artifacts) == 0
        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.task_state_completed
        # stream chunks + final artifact
        assert len(loaded.artifacts) >= 1
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_fail_drains_buffer():
    """fail() persists pending artifacts alongside the failure."""
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        ctx._flush_interval = 999
        ctx._flush_count = 999
        ctx._last_flush = __import__("time").monotonic()

        await ctx.emit_text_artifact("partial", artifact_id="stream", append=True)
        await ctx.fail("something went wrong")

        assert len(ctx._pending_artifacts) == 0
        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.task_state_failed
        assert len(loaded.artifacts) >= 1
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_send_status_flushes_pending_with_status():
    """send_status with text piggybacks pending artifacts into the DB write."""
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        ctx._flush_interval = 999
        ctx._flush_count = 999
        ctx._last_flush = __import__("time").monotonic()

        await ctx.emit_text_artifact("chunk", artifact_id="s", append=True)
        assert len(ctx._pending_artifacts) == 1

        await ctx.send_status("thinking...")

        # Pending flushed together with the status write
        assert len(ctx._pending_artifacts) == 0
        loaded = await storage.load_task(task.id)
        assert len(loaded.artifacts) >= 1
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_sse_fires_before_db_for_artifacts():
    """SSE event is sent before any DB write for emit_artifact."""
    ctx, _storage, event_bus, _task = await _make_ctx()
    try:
        ctx._flush_interval = 999
        ctx._flush_count = 999
        ctx._last_flush = __import__("time").monotonic()

        call_order: list[str] = []
        original_send_event = ctx._emitter.send_event
        original_update_task = ctx._emitter.update_task

        async def tracked_send_event(*args, **kwargs):
            call_order.append("send_event")
            return await original_send_event(*args, **kwargs)

        async def tracked_update_task(*args, **kwargs):
            call_order.append("update_task")
            return await original_update_task(*args, **kwargs)

        ctx._emitter.send_event = tracked_send_event
        ctx._emitter.update_task = tracked_update_task

        await ctx.emit_text_artifact("hello", artifact_id="test")

        # SSE sent, no DB write (buffered, below threshold)
        assert call_order == ["send_event"]
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_terminal_db_before_sse():
    """complete() writes DB before sending SSE with final=True."""
    ctx, _storage, event_bus, _task = await _make_ctx()
    try:
        call_order: list[str] = []
        original_send_event = ctx._emitter.send_event
        original_update_task = ctx._emitter.update_task

        async def tracked_send_event(*args, **kwargs):
            call_order.append("send_event")
            return await original_send_event(*args, **kwargs)

        async def tracked_update_task(*args, **kwargs):
            call_order.append("update_task")
            return await original_update_task(*args, **kwargs)

        ctx._emitter.send_event = tracked_send_event
        ctx._emitter.update_task = tracked_update_task

        await ctx.complete("done")

        assert call_order[0] == "update_task"
        assert "send_event" in call_order[1:]
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_polling_sees_terminal_with_all_artifacts():
    """Polling after complete() sees all artifacts including buffered chunks."""
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        ctx._flush_interval = 999
        ctx._flush_count = 999
        ctx._last_flush = __import__("time").monotonic()

        for i in range(3):
            await ctx.emit_text_artifact(f"word{i}", artifact_id="stream", append=True)

        await ctx.complete("final answer")

        loaded = await storage.load_task(task.id)
        assert loaded.status.state == TaskState.task_state_completed
        # 3 streamed chunks + 1 final artifact
        assert len(loaded.artifacts) >= 1
    finally:
        await event_bus.__aexit__(None, None, None)


async def test_send_status_persists_for_polling_clients():
    """send_status(message) persists the status message so tasks/get sees progress."""
    ctx, storage, event_bus, task = await _make_ctx()
    try:
        await ctx.emit_text_artifact("chunk", artifact_id="s", append=True)
        await ctx.send_status("thinking...")

        # Status write flushed the buffered artifact along with the message
        assert len(ctx._pending_artifacts) == 0
        loaded = await storage.load_task(task.id)
        assert loaded.status.message is not None
        assert len(loaded.artifacts) == 1
    finally:
        await event_bus.__aexit__(None, None, None)
