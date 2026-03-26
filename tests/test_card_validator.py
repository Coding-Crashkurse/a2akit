"""Tests for the card_validator hook on A2AClient."""

from __future__ import annotations

import httpx
import pytest
from asgi_lifespan import LifespanManager

from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig
from a2akit.client import A2AClient
from conftest import EchoWorker


def _make_app():
    server = A2AServer(
        worker=EchoWorker(),
        agent_card=AgentCardConfig(
            name="Test Agent",
            description="Test agent",
            version="0.0.1",
            protocol="http+json",
            capabilities=CapabilitiesConfig(streaming=False),
        ),
    )
    return server.as_fastapi_app()


@pytest.fixture
async def server_http():
    """Yield an httpx.AsyncClient wired to an in-process server."""
    app = _make_app()
    async with LifespanManager(app) as manager:
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=manager.app),
            base_url="http://test",
        )
        yield http
        await http.aclose()


class TestCardValidator:
    async def test_no_validator_default(self, server_http):
        """Client without card_validator connects normally."""
        client = A2AClient("http://test", httpx_client=server_http)
        await client.connect()
        assert client.is_connected
        await client.close()

    async def test_validator_called_with_correct_args(self, server_http):
        """Validator receives (AgentCard, bytes)."""
        received = {}

        def capture(card, raw_body):
            received["card"] = card
            received["raw_body"] = raw_body

        client = A2AClient("http://test", httpx_client=server_http, card_validator=capture)
        await client.connect()

        from a2a.types import AgentCard

        assert isinstance(received["card"], AgentCard)
        assert isinstance(received["raw_body"], bytes)
        assert received["card"].name == "Test Agent"
        await client.close()

    async def test_validator_passes_allows_connection(self, server_http):
        """Validator returning None → client connects."""

        def noop(card, raw_body):
            pass

        client = A2AClient("http://test", httpx_client=server_http, card_validator=noop)
        await client.connect()
        assert client.is_connected
        await client.close()

    async def test_validator_raises_blocks_connection(self, server_http):
        """Validator raising → connect() raises, client stays disconnected."""

        def reject(card, raw_body):
            raise ValueError("Untrusted agent")

        client = A2AClient("http://test", httpx_client=server_http, card_validator=reject)
        with pytest.raises(ValueError, match="Untrusted agent"):
            await client.connect()
        assert not client.is_connected

    async def test_validator_exception_type_preserved(self, server_http):
        """Custom exception type propagates without wrapping."""

        class SecurityError(Exception):
            pass

        def reject(card, raw_body):
            raise SecurityError("Bad signature")

        client = A2AClient("http://test", httpx_client=server_http, card_validator=reject)
        with pytest.raises(SecurityError, match="Bad signature"):
            await client.connect()

    async def test_validator_receives_raw_bytes(self, server_http):
        """Raw bytes match resp.content, not card.model_dump_json()."""
        received_bytes = {}

        def capture(card, raw_body):
            received_bytes["raw"] = raw_body
            received_bytes["reserialized"] = card.model_dump_json().encode()

        client = A2AClient("http://test", httpx_client=server_http, card_validator=capture)
        await client.connect()

        # Raw bytes are valid JSON that parses to the same card
        import json

        parsed = json.loads(received_bytes["raw"])
        assert parsed["name"] == "Test Agent"

        # Raw bytes may differ from Pydantic re-serialization
        assert isinstance(received_bytes["raw"], bytes)
        await client.close()

    async def test_context_manager_with_failing_validator(self, server_http):
        """async with + failing validator raises during __aenter__, cleans up."""

        def reject(card, raw_body):
            raise ValueError("Nope")

        with pytest.raises(ValueError, match="Nope"):
            async with A2AClient("http://test", httpx_client=server_http, card_validator=reject):
                pass  # should never reach here

    async def test_validator_with_real_server(self):
        """End-to-end: start server, connect with validator that inspects card.name."""
        app = _make_app()
        async with LifespanManager(app) as manager:
            http = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=manager.app),
                base_url="http://test",
            )

            names_seen = []

            def check_name(card, raw_body):
                names_seen.append(card.name)
                if card.name != "Test Agent":
                    raise ValueError(f"Unexpected agent: {card.name}")

            async with A2AClient(
                "http://test", httpx_client=http, card_validator=check_name
            ) as client:
                result = await client.send("hello")
                assert result.text == "Echo: hello"

            assert names_seen == ["Test Agent"]
            await http.aclose()
