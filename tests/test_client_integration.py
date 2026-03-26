"""Integration tests for A2AClient against real server apps.

Tests run against both HTTP+JSON and JSON-RPC protocols.
"""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig
from a2akit.client import (
    A2AClient,
    AgentCapabilityError,
    AgentNotFoundError,
    NotConnectedError,
    TaskNotCancelableError,
    TaskNotFoundError,
)
from conftest import (
    DirectReplyWorker,
    EchoWorker,
    FailWorker,
    InputRequiredWorker,
    JsonCompleteWorker,
    RejectWorker,
    StreamingWorker,
)


def _make_app(worker, protocol="http+json", streaming=True):
    server = A2AServer(
        worker=worker,
        agent_card=AgentCardConfig(
            name="Test Agent",
            description="Test agent",
            version="0.0.1",
            protocol=protocol,
            capabilities=CapabilitiesConfig(streaming=streaming),
        ),
    )
    return server.as_fastapi_app()


async def _make_client(worker, protocol="http+json", streaming=True):
    """Create a connected A2AClient against an in-process server."""
    app = _make_app(worker, protocol=protocol, streaming=streaming)
    manager = LifespanManager(app)
    await manager.__aenter__()
    http_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=manager.app),
        base_url="http://test",
    )
    client = A2AClient("http://test", httpx_client=http_client, protocol=protocol)
    await client.connect()
    return client, manager, http_client


@pytest.fixture(params=["http+json", "jsonrpc"])
async def echo_client(request):
    """A2AClient connected to EchoWorker, parametrized by protocol."""
    client, manager, http = await _make_client(EchoWorker(), protocol=request.param)
    yield client
    await client.close()
    await http.aclose()
    await manager.__aexit__(None, None, None)


@pytest.fixture(params=["http+json", "jsonrpc"])
async def streaming_client(request):
    """A2AClient connected to StreamingWorker."""
    client, manager, http = await _make_client(StreamingWorker(), protocol=request.param)
    yield client
    await client.close()
    await http.aclose()
    await manager.__aexit__(None, None, None)


@pytest.fixture(params=["http+json", "jsonrpc"])
async def fail_client(request):
    """A2AClient connected to FailWorker."""
    client, manager, http = await _make_client(FailWorker(), protocol=request.param)
    yield client
    await client.close()
    await http.aclose()
    await manager.__aexit__(None, None, None)


@pytest.fixture(params=["http+json", "jsonrpc"])
async def reject_client(request):
    """A2AClient connected to RejectWorker."""
    client, manager, http = await _make_client(RejectWorker(), protocol=request.param)
    yield client
    await client.close()
    await http.aclose()
    await manager.__aexit__(None, None, None)


@pytest.fixture(params=["http+json", "jsonrpc"])
async def json_client(request):
    """A2AClient connected to JsonCompleteWorker."""
    client, manager, http = await _make_client(JsonCompleteWorker(), protocol=request.param)
    yield client
    await client.close()
    await http.aclose()
    await manager.__aexit__(None, None, None)


@pytest.fixture(params=["http+json", "jsonrpc"])
async def input_client(request):
    """A2AClient connected to InputRequiredWorker."""
    client, manager, http = await _make_client(InputRequiredWorker(), protocol=request.param)
    yield client
    await client.close()
    await http.aclose()
    await manager.__aexit__(None, None, None)


@pytest.fixture(params=["http+json", "jsonrpc"])
async def direct_reply_client(request):
    """A2AClient connected to DirectReplyWorker."""
    client, manager, http = await _make_client(DirectReplyWorker(), protocol=request.param)
    yield client
    await client.close()
    await http.aclose()
    await manager.__aexit__(None, None, None)


class TestConnect:
    async def test_connect_context_manager(self):
        app = _make_app(EchoWorker())
        async with LifespanManager(app) as manager:
            http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=manager.app),
                base_url="http://test",
            )
            async with A2AClient("http://test", httpx_client=http) as client:
                assert client.is_connected
                assert client.agent_name == "Test Agent"
            assert not client.is_connected
            await http.aclose()

    async def test_connect_rest_protocol(self):
        client, manager, http = await _make_client(EchoWorker(), protocol="http+json")
        assert client.protocol == "http+json"
        await client.close()
        await http.aclose()
        await manager.__aexit__(None, None, None)

    async def test_connect_jsonrpc_protocol(self):
        client, manager, http = await _make_client(EchoWorker(), protocol="jsonrpc")
        assert client.protocol == "jsonrpc"
        await client.close()
        await http.aclose()
        await manager.__aexit__(None, None, None)

    async def test_not_connected_error(self):
        client = A2AClient("http://localhost:9999")
        with pytest.raises(NotConnectedError):
            await client.send("hello")

    async def test_close_idempotent(self):
        client, manager, http = await _make_client(EchoWorker())
        await client.close()
        await client.close()  # should not raise
        await http.aclose()
        await manager.__aexit__(None, None, None)

    async def test_properties_after_connect(self):
        client, manager, http = await _make_client(EchoWorker())
        assert client.agent_name == "Test Agent"
        assert client.capabilities is not None
        assert client.capabilities.streaming is True
        assert client.is_connected
        await client.close()
        await http.aclose()
        await manager.__aexit__(None, None, None)

    async def test_no_agent_card(self):
        """Connecting to a URL without an agent card raises AgentNotFoundError."""
        http = httpx.AsyncClient(base_url="http://test")
        client = A2AClient("http://test", httpx_client=http)
        with pytest.raises(AgentNotFoundError):
            await client.connect()
        await http.aclose()


class TestSend:
    async def test_send_echo(self, echo_client):
        result = await echo_client.send("hello")
        assert result.text == "Echo: hello"
        assert result.completed
        assert result.task_id

    async def test_send_blocking(self, echo_client):
        result = await echo_client.send("hi", blocking=True)
        assert result.completed

    async def test_send_fail_worker(self, fail_client):
        result = await fail_client.send("hi")
        assert result.failed
        assert result.is_terminal

    async def test_send_reject_worker(self, reject_client):
        result = await reject_client.send("hi")
        assert result.rejected
        assert result.is_terminal

    async def test_send_json_complete(self, json_client):
        result = await json_client.send("hi")
        assert result.data is not None
        assert result.data["result"] == "ok"
        assert result.data["value"] == 42

    async def test_send_with_metadata(self, echo_client):
        result = await echo_client.send("hi", metadata={"key": "val"})
        assert result.completed


class TestStream:
    async def test_stream_words(self, streaming_client):
        events = []
        async for event in streaming_client.stream("hello world"):
            events.append(event)
        assert len(events) > 0
        # Should have at least task + status + artifact events
        kinds = {e.kind for e in events}
        assert "task" in kinds or "status" in kinds or "artifact" in kinds

    async def test_stream_has_artifact_events(self, streaming_client):
        artifact_events = []
        async for event in streaming_client.stream("hello world"):
            if event.kind == "artifact":
                artifact_events.append(event)
        assert len(artifact_events) > 0

    async def test_stream_text(self, streaming_client):
        chunks = []
        async for chunk in streaming_client.stream_text("hello world"):
            chunks.append(chunk)
        assert len(chunks) > 0
        joined = "".join(chunks)
        assert "hello" in joined
        assert "world" in joined

    async def test_stream_not_supported(self):
        """Non-streaming agent raises AgentCapabilityError."""
        client, manager, http = await _make_client(EchoWorker(), streaming=False)
        with pytest.raises(AgentCapabilityError):
            async for _ in client.stream("hello"):
                pass
        await client.close()
        await http.aclose()
        await manager.__aexit__(None, None, None)


class TestTasks:
    async def test_get_task_exists(self, echo_client):
        result = await echo_client.send("hello")
        fetched = await echo_client.get_task(result.task_id)
        assert fetched.task_id == result.task_id
        assert fetched.completed

    async def test_get_task_not_found(self, echo_client):
        with pytest.raises(TaskNotFoundError):
            await echo_client.get_task("nonexistent-task-id")

    async def test_cancel_completed_task(self, echo_client):
        result = await echo_client.send("hello")
        with pytest.raises(TaskNotCancelableError):
            await echo_client.cancel(result.task_id)

    async def test_cancel_not_found(self, echo_client):
        with pytest.raises(TaskNotFoundError):
            await echo_client.cancel("nonexistent-task-id")


class TestMultiTurn:
    async def test_input_required_follow_up(self, input_client):
        result = await input_client.send("Book a flight")
        assert result.input_required

        result2 = await input_client.send(
            "SF to NY",
            task_id=result.task_id,
            context_id=result.context_id,
        )
        assert result2.completed
        assert "Got follow-up" in (result2.text or "")


class TestDirectReply:
    async def test_direct_reply_returns_message(self, direct_reply_client):
        result = await direct_reply_client.send("hi")
        assert result.text is not None
        assert "Direct: hi" in result.text


class TestProtocolDetect:
    async def test_auto_detect_rest(self):
        """Card with http+json preferred → auto-selects http+json."""
        client, manager, http = await _make_client(EchoWorker(), protocol="http+json")
        # Force auto-detect by creating a new client without explicit protocol
        client2 = A2AClient("http://test", httpx_client=http)
        await client2.connect()
        # The server card has http+json preferred, so auto-detect should pick it
        assert client2.protocol == "http+json"
        await client2.close()
        await client.close()
        await http.aclose()
        await manager.__aexit__(None, None, None)

    async def test_auto_detect_jsonrpc(self):
        """Card with jsonrpc preferred → auto-selects jsonrpc."""
        client, manager, http = await _make_client(EchoWorker(), protocol="jsonrpc")
        client2 = A2AClient("http://test", httpx_client=http)
        await client2.connect()
        assert client2.protocol == "jsonrpc"
        await client2.close()
        await client.close()
        await http.aclose()
        await manager.__aexit__(None, None, None)

    async def test_forced_protocol_overrides(self):
        """Explicit protocol= wins over card preference."""
        app = _make_app(EchoWorker(), protocol="http+json")
        async with LifespanManager(app) as manager:
            http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=manager.app),
                base_url="http://test",
            )
            # Server is http+json but client forces jsonrpc
            # (This will work for connect but requests may fail — we just test detection)
            client = A2AClient("http://test", httpx_client=http, protocol="jsonrpc")
            await client.connect()
            assert client.protocol == "jsonrpc"
            await client.close()
            await http.aclose()
