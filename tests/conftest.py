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
    CapabilitiesConfig,
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


def _make_app(worker: Worker, *, streaming: bool = False, **server_kwargs: object) -> FastAPI:
    """Create a FastAPI app with the given worker."""
    server = A2AServer(
        worker=worker,
        agent_card=AgentCardConfig(
            name="Test Agent",
            description="Test agent for unit tests",
            version="0.0.1",
            protocol="http+json",
            capabilities=CapabilitiesConfig(streaming=streaming),
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
    raw_app = _make_app(StreamingWorker(), streaming=True)
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


@pytest.fixture(params=["sqlite", "postgres"])
async def sql_storage(request):
    """Parametrized fixture — runs against both SQL backends."""
    if request.param == "sqlite":
        try:
            from a2akit.storage.sqlite import SQLiteStorage
        except ImportError:
            pytest.skip("aiosqlite not installed")

        s = SQLiteStorage("sqlite+aiosqlite://")
        async with s:
            yield s
    else:
        import os

        url = os.environ.get("A2AKIT_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("PostgreSQL not configured (set A2AKIT_TEST_POSTGRES_URL)")

        try:
            from a2akit.storage._sql_base import contexts_table, tasks_table
            from a2akit.storage.postgres import PostgreSQLStorage
        except ImportError:
            pytest.skip("asyncpg not installed")

        s = PostgreSQLStorage(url)
        async with s:
            yield s
            async with s._get_session() as session, session.begin():
                await session.execute(tasks_table.delete())
                await session.execute(contexts_table.delete())


@pytest.fixture(params=["memory", "redis"])
async def broker(request):
    """Parametrized broker — runs against both InMemory and Redis backends."""
    if request.param == "memory":
        b = InMemoryBroker()
        async with b:
            yield b
    else:
        import os

        url = os.environ.get("A2AKIT_TEST_REDIS_URL")
        if not url:
            pytest.skip("Redis not configured (set A2AKIT_TEST_REDIS_URL)")

        try:
            from a2akit.broker.redis import RedisBroker
        except ImportError:
            pytest.skip("redis-py not installed (pip install a2akit[redis])")

        b = RedisBroker(url, key_prefix=f"a2akit:test:{uuid.uuid4().hex[:8]}:")
        async with b:
            yield b
            # Cleanup test keys
            await b._redis.flushdb()


@pytest.fixture(params=["memory", "redis"])
async def event_bus(request):
    """Parametrized event bus — runs against both InMemory and Redis backends."""
    if request.param == "memory":
        eb = InMemoryEventBus()
        async with eb:
            yield eb
    else:
        import os

        url = os.environ.get("A2AKIT_TEST_REDIS_URL")
        if not url:
            pytest.skip("Redis not configured (set A2AKIT_TEST_REDIS_URL)")

        try:
            from a2akit.event_bus.redis import RedisEventBus
        except ImportError:
            pytest.skip("redis-py not installed (pip install a2akit[redis])")

        eb = RedisEventBus(url, key_prefix=f"a2akit:test:{uuid.uuid4().hex[:8]}:")
        async with eb:
            yield eb
            await eb._redis.flushdb()


@pytest.fixture(params=["memory", "redis"])
async def cancel_registry(request):
    """Parametrized cancel registry — runs against both backends."""
    if request.param == "memory":
        from a2akit.broker.memory import InMemoryCancelRegistry

        yield InMemoryCancelRegistry()
    else:
        import os

        url = os.environ.get("A2AKIT_TEST_REDIS_URL")
        if not url:
            pytest.skip("Redis not configured (set A2AKIT_TEST_REDIS_URL)")

        try:
            from a2akit.broker.redis import RedisCancelRegistry
        except ImportError:
            pytest.skip("redis-py not installed (pip install a2akit[redis])")

        cr = RedisCancelRegistry(url, key_prefix=f"a2akit:test:{uuid.uuid4().hex[:8]}:")
        yield cr
        await cr.close()


@pytest.fixture
def otel_setup():
    """Setup InMemorySpanExporter for OTel tests.

    Properly resets the global TracerProvider so tests don't interfere.
    """
    from opentelemetry import trace as trace_api
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Force-reset the global provider (OTel only allows set_tracer_provider once)
    trace_api._TRACER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    trace_api._TRACER_PROVIDER = None  # type: ignore[attr-defined]
    trace_api.set_tracer_provider(provider)

    # Reset a2akit's lazy singletons so they pick up the new provider
    import a2akit.telemetry._instruments as inst

    old_enabled = inst.OTEL_ENABLED
    inst.OTEL_ENABLED = True
    inst._tracer = None
    inst._meter = None

    yield exporter

    inst.OTEL_ENABLED = old_enabled
    inst._tracer = None
    inst._meter = None
    exporter.clear()
    provider.shutdown()


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
