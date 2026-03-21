"""Unit tests for CapabilitiesConfig validation."""

from __future__ import annotations

from a2a.types import AgentExtension

from a2akit.agent_card import CapabilitiesConfig


def test_default_capabilities():
    """All capabilities default to False, no error."""
    caps = CapabilitiesConfig()
    assert caps.streaming is False
    assert caps.push_notifications is False
    assert caps.extended_agent_card is False
    assert caps.extensions is None


def test_streaming_enabled():
    """streaming=True works without error."""
    caps = CapabilitiesConfig(streaming=True)
    assert caps.streaming is True


def test_push_notifications_enabled():
    """push_notifications=True works without error."""
    caps = CapabilitiesConfig(push_notifications=True)
    assert caps.push_notifications is True


def test_extended_agent_card_accepted():
    """extended_agent_card=True is now accepted (no longer raises)."""
    caps = CapabilitiesConfig(extended_agent_card=True)
    assert caps.extended_agent_card is True


def test_extensions_accepted():
    """extensions=[...] is accepted without error."""
    ext = AgentExtension(uri="urn:example:ext")
    caps = CapabilitiesConfig(extensions=[ext])
    assert caps.extensions is not None
    assert len(caps.extensions) == 1
    assert caps.extensions[0].uri == "urn:example:ext"
