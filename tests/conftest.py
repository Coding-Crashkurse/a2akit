"""Shared fixtures for a2akit tests."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import httpx
import pytest
from asgi_lifespan import LifespanManager

from a2akit import (
    A2AServer,
    AgentCardConfig,
    InMemoryBroker,
    InMemoryEventBus,
    InMemoryStorage,
    TaskContext,
    Worker,
)

if TYPE_CHECKING:
    from fastapi import FastAPI


class EchoWorker(Worker):
    """Test worker that echoes input back."""

    async def handle(self, ctx: TaskContext) -> None:
        """Echo the user text back."""
        await ctx.complete(f"Echo: {ctx.user_text}")


class StreamingWorker(Worker):
    """Test worker that streams words one by one."""

    async def handle(self, ctx: TaskContext) -> None:
        """Stream words as artifact chunks."""
        words = ctx.user_text.split()
        await ctx.send_status(f"Streaming {len(words)} words...")

        for i, word in enumerate(words):
            is_last = i == len(words) - 1
            await ctx.emit_text_artifact(
                text=word + ("" if is_last else " "),
                artifact_id="stream",
                append=(i > 0),
                last_chunk=is_last,
            )

        await ctx.complete()


class FailWorker(Worker):
    """Test worker that always fails."""

    async def handle(self, ctx: TaskContext) -> None:
        """Fail with a reason."""
        await ctx.fail("Something went wrong")


class RejectWorker(Worker):
    """Test worker that rejects tasks."""

    async def handle(self, ctx: TaskContext) -> None:
        """Reject with a reason."""
        await ctx.reject("Not my job")


class InputRequiredWorker(Worker):
    """Test worker that requests additional input."""

    async def handle(self, ctx: TaskContext) -> None:
        """Request input or complete based on history."""
        if ctx.history:
            await ctx.complete(f"Got follow-up: {ctx.user_text}")
        else:
            await ctx.request_input("What is your name?")


class DirectReplyWorker(Worker):
    """Test worker that uses reply_directly."""

    async def handle(self, ctx: TaskContext) -> None:
        """Reply directly without task tracking."""
        await ctx.reply_directly(f"Direct: {ctx.user_text}")


class RespondWorker(Worker):
    """Test worker that uses respond() (no artifact)."""

    async def handle(self, ctx: TaskContext) -> None:
        """Respond without creating an artifact."""
        await ctx.respond(f"Response: {ctx.user_text}")


class NoLifecycleWorker(Worker):
    """Test worker that returns without calling a lifecycle method."""

    async def handle(self, ctx: TaskContext) -> None:
        """Return without calling complete/fail/etc."""


class CrashWorker(Worker):
    """Test worker that raises an exception."""

    async def handle(self, ctx: TaskContext) -> None:
        """Raise an unhandled exception."""
        raise RuntimeError("Worker crashed!")


class ContextWorker(Worker):
    """Test worker that uses context load/update."""

    async def handle(self, ctx: TaskContext) -> None:
        """Load context, increment counter, save, and complete."""
        data = await ctx.load_context() or {"count": 0}
        data["count"] += 1
        await ctx.update_context(data)
        await ctx.complete(f"Count: {data['count']}")


class ArtifactWorker(Worker):
    """Test worker that emits various artifact types."""

    async def handle(self, ctx: TaskContext) -> None:
        """Emit text, data, and file artifacts."""
        await ctx.emit_text_artifact("hello", artifact_id="text-art")
        await ctx.emit_data_artifact({"key": "value"}, artifact_id="data-art")
        await ctx.emit_artifact(
            artifact_id="file-art",
            file_bytes=b"binary content",
            media_type="application/octet-stream",
            filename="test.bin",
        )
        await ctx.complete()


class JsonCompleteWorker(Worker):
    """Test worker that completes with JSON."""

    async def handle(self, ctx: TaskContext) -> None:
        """Complete with a JSON data artifact."""
        await ctx.complete_json({"result": "ok", "value": 42})


class AuthRequiredWorker(Worker):
    """Test worker that requests authentication."""

    async def handle(self, ctx: TaskContext) -> None:
        """Request auth or complete on follow-up."""
        if ctx.history:
            await ctx.complete(f"Authenticated: {ctx.user_text}")
        else:
            await ctx.request_auth("Please provide API key")


class StatusWorker(Worker):
    """Test worker that sends intermediate statuses before completing."""

    async def handle(self, ctx: TaskContext) -> None:
        """Send status updates then complete."""
        await ctx.send_status("Step 1")
        await ctx.send_status("Step 2")
        await ctx.complete("Done")


def _make_app(worker: Worker, **server_kwargs) -> FastAPI:
    """Create a FastAPI app with the given worker."""
    server = A2AServer(
        worker=worker,
        agent_card=AgentCardConfig(
            name="Test Agent",
            description="Test agent for unit tests",
            version="0.0.1",
        ),
        **server_kwargs,
    )
    return server.as_fastapi_app()


@pytest.fixture
async def app():
    """Complete FastAPI app with EchoWorker (lifespan started)."""
    raw_app = _make_app(EchoWorker())
    async with LifespanManager(raw_app) as manager:
        yield manager.app


@pytest.fixture
async def client(app):
    """httpx.AsyncClient against the app (ASGITransport)."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def streaming_app():
    """App with StreamingWorker for SSE tests (lifespan started)."""
    raw_app = _make_app(StreamingWorker())
    async with LifespanManager(raw_app) as manager:
        yield manager.app


@pytest.fixture
async def streaming_client(streaming_app):
    """httpx.AsyncClient for streaming app."""
    transport = httpx.ASGITransport(app=streaming_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def storage():
    """Isolated InMemoryStorage instance."""
    return InMemoryStorage()


@pytest.fixture
async def broker():
    """Isolated InMemoryBroker instance (entered)."""
    b = InMemoryBroker()
    async with b:
        yield b


@pytest.fixture
async def event_bus():
    """Isolated InMemoryEventBus instance (entered)."""
    eb = InMemoryEventBus()
    async with eb:
        yield eb


@pytest.fixture
def make_send_params():
    """Factory for MessageSendParams dicts with defaults."""

    def _make(
        text: str = "hello",
        message_id: str | None = None,
        task_id: str | None = None,
        context_id: str | None = None,
        blocking: bool = True,
    ) -> dict:
        msg = {
            "role": "user",
            "messageId": message_id or str(uuid.uuid4()),
            "parts": [{"kind": "text", "text": text}],
        }
        if task_id:
            msg["taskId"] = task_id
        if context_id:
            msg["contextId"] = context_id

        body: dict = {"message": msg}
        if blocking:
            body["configuration"] = {"blocking": True}
        return body

    return _make
