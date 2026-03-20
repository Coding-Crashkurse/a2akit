"""Tests for AgentExtension / CapabilitiesConfig extensions support."""

from __future__ import annotations

import httpx
from a2a.types import AgentExtension
from asgi_lifespan import LifespanManager

from a2akit import (
    A2AServer,
    AgentCardConfig,
    CapabilitiesConfig,
    ExtensionConfig,
    TaskContext,
    Worker,
)
from a2akit.agent_card import build_agent_card

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


def _make_extensions_app(extensions: list[ExtensionConfig] | None = None):
    caps = CapabilitiesConfig(
        extensions=[
            AgentExtension(
                uri=e.uri,
                description=e.description,
                required=e.required or None,
                params=e.params or None,
            )
            for e in extensions
        ]
        if extensions
        else None,
    )
    server = A2AServer(
        worker=_EchoWorker(),
        agent_card=AgentCardConfig(
            name="Ext Test Agent",
            description="Agent for extension tests.",
            version="0.0.1",
            protocol="http+json",
            capabilities=caps,
            extensions=extensions or [],
        ),
    )
    return server.as_fastapi_app()


# ---------------------------------------------------------------------------
# Unit tests — no HTTP, no lifespan
# ---------------------------------------------------------------------------


def test_extensions_config_accepted():
    """CapabilitiesConfig(extensions=[...]) does NOT raise NotImplementedError."""
    ext = AgentExtension(uri="urn:example:ext")
    caps = CapabilitiesConfig(extensions=[ext])
    assert caps.extensions is not None
    assert len(caps.extensions) == 1


def test_extensions_empty_list():
    """CapabilitiesConfig(extensions=[]) works fine (treated as no extensions)."""
    caps = CapabilitiesConfig(extensions=[])
    assert caps.extensions == []


def test_extensions_none_default():
    """CapabilitiesConfig() has extensions=None."""
    caps = CapabilitiesConfig()
    assert caps.extensions is None


def test_extension_config_fields():
    """ExtensionConfig stores all fields correctly."""
    ext = ExtensionConfig(uri="urn:x", description="d", params={"k": "v"})
    assert ext.uri == "urn:x"
    assert ext.description == "d"
    assert ext.params == {"k": "v"}


def test_extension_config_defaults():
    """ExtensionConfig defaults: description=None, params={}, required=False."""
    ext = ExtensionConfig(uri="urn:x")
    assert ext.description is None
    assert ext.params == {}
    assert ext.required is False


def test_extension_required_field():
    """ExtensionConfig(uri='urn:x', required=True) stores required=True."""
    ext = ExtensionConfig(uri="urn:x", required=True)
    assert ext.required is True


def test_build_agent_card_with_extensions():
    """build_agent_card() with extensions populates capabilities.extensions."""
    config = AgentCardConfig(
        name="Test",
        description="Test agent",
        version="0.1.0",
        extensions=[
            ExtensionConfig(
                uri="urn:example:logging",
                description="Logging ext",
                params={"level": "debug"},
            ),
            ExtensionConfig(uri="urn:example:metrics", required=True),
        ],
    )
    card = build_agent_card(config, "http://localhost:8000")
    assert card.capabilities is not None
    exts = card.capabilities.extensions
    assert exts is not None
    assert len(exts) == 2
    assert exts[0].uri == "urn:example:logging"
    assert exts[0].description == "Logging ext"
    assert exts[0].params == {"level": "debug"}
    assert exts[1].uri == "urn:example:metrics"
    assert exts[1].required is True


def test_build_agent_card_without_extensions():
    """Default config results in card.capabilities.extensions being None."""
    config = AgentCardConfig(
        name="Test",
        description="Test agent",
        version="0.1.0",
    )
    card = build_agent_card(config, "http://localhost:8000")
    assert card.capabilities is not None
    assert card.capabilities.extensions is None


# ---------------------------------------------------------------------------
# Integration tests — HTTP round-trip
# ---------------------------------------------------------------------------


async def test_extensions_visible_in_agent_card_http():
    """Extensions appear in the agent card JSON response."""
    raw_app = _make_extensions_app(
        extensions=[
            ExtensionConfig(
                uri="urn:example:custom-logging",
                description="Structured logging",
                params={"format": "json"},
            ),
            ExtensionConfig(uri="urn:example:rate-limiting", required=True),
        ],
    )
    async with LifespanManager(raw_app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/.well-known/agent-card.json")
            assert resp.status_code == 200
            data = resp.json()
            exts = data["capabilities"]["extensions"]
            assert isinstance(exts, list)
            assert len(exts) == 2
            assert exts[0]["uri"] == "urn:example:custom-logging"
            assert exts[0]["description"] == "Structured logging"
            assert exts[0]["params"] == {"format": "json"}
            assert exts[1]["uri"] == "urn:example:rate-limiting"
            assert exts[1]["required"] is True


async def test_extensions_not_in_agent_card_when_none():
    """Without extensions, capabilities.extensions is null or absent."""
    raw_app = _make_extensions_app(extensions=None)
    async with LifespanManager(raw_app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/.well-known/agent-card.json")
            assert resp.status_code == 200
            data = resp.json()
            caps = data["capabilities"]
            assert caps.get("extensions") is None
