"""Tests for AgentCard builder and external_base_url utility."""

from __future__ import annotations

from a2akit.agent_card import (
    AgentCardConfig,
    SkillConfig,
    build_agent_card,
    external_base_url,
)


async def test_build_agent_card_basic():
    config = AgentCardConfig(name="Test", description="A test agent", version="1.0.0")
    card = build_agent_card(config, "http://localhost:8000")
    assert card.name == "Test"
    assert card.description == "A test agent"
    assert card.version == "1.0.0"
    assert card.url == "http://localhost:8000/v1"
    assert card.protocol_version == "0.3.0"


async def test_build_agent_card_with_skills():
    config = AgentCardConfig(
        name="Skilled",
        description="Has skills",
        version="1.0.0",
        skills=[SkillConfig(id="s1", name="Skill One", description="Does stuff", tags=["tag"])],
    )
    card = build_agent_card(config, "http://localhost:8000")
    assert len(card.skills) == 1
    assert card.skills[0].id == "s1"


async def test_external_base_url_plain():
    url = external_base_url({}, "http", "localhost:8000")
    assert url == "http://localhost:8000"


async def test_external_base_url_forwarded():
    url = external_base_url(
        {"x-forwarded-proto": "https", "x-forwarded-host": "api.example.com"},
        "http",
        "localhost:8000",
    )
    assert url == "https://api.example.com"
