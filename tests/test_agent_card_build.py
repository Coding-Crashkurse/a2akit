"""Tests for AgentCard builder and external_base_url utility."""

from __future__ import annotations

import pytest
from a2a_pydantic.v03 import HTTPAuthSecurityScheme

from a2akit.agent_card import (
    AgentCardConfig,
    ProviderConfig,
    SignatureConfig,
    SkillConfig,
    build_agent_card,
    external_base_url,
)


async def test_build_agent_card_basic():
    config = AgentCardConfig(name="Test", description="A test agent", version="1.0.0")
    # Explicit v1.0 — conftest pins the framework default to 0.3 for legacy
    # test compatibility.
    card = build_agent_card(config, "http://localhost:8000", protocol_version="1.0")
    assert card.name == "Test"
    assert card.description == "A test agent"
    assert card.version == "1.0.0"
    # v10 card exposes supported_interfaces[] instead of url
    assert any(i.url == "http://localhost:8000" for i in card.supported_interfaces)


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


async def test_external_base_url_forwarded_ignored_by_default():
    """X-Forwarded-* headers are spoofable; ignored unless explicitly trusted."""
    url = external_base_url(
        {"x-forwarded-proto": "https", "x-forwarded-host": "api.example.com"},
        "http",
        "localhost:8000",
    )
    assert url == "http://localhost:8000"


async def test_external_base_url_forwarded_with_trust_proxy_headers(monkeypatch):
    from a2akit.config import get_settings

    monkeypatch.setenv("A2AKIT_TRUST_PROXY_HEADERS", "true")
    get_settings.cache_clear()
    try:
        url = external_base_url(
            {"x-forwarded-proto": "https", "x-forwarded-host": "api.example.com"},
            "http",
            "localhost:8000",
        )
        assert url == "https://api.example.com"
    finally:
        get_settings.cache_clear()


async def test_build_card_with_provider():
    config = AgentCardConfig(
        name="P",
        description="d",
        provider=ProviderConfig(organization="Acme", url="https://acme.example.com"),
    )
    card = build_agent_card(config, "http://localhost:8000")
    assert card.provider is not None
    assert card.provider.organization == "Acme"
    assert card.provider.url == "https://acme.example.com"


async def test_build_card_without_provider():
    config = AgentCardConfig(name="P", description="d")
    card = build_agent_card(config, "http://localhost:8000")
    assert card.provider is None


async def test_build_card_with_icon_url():
    config = AgentCardConfig(name="I", description="d", icon_url="https://example.com/icon.png")
    card = build_agent_card(config, "http://localhost:8000")
    assert card.icon_url == "https://example.com/icon.png"


async def test_build_card_with_documentation_url():
    config = AgentCardConfig(
        name="D", description="d", documentation_url="https://docs.example.com"
    )
    card = build_agent_card(config, "http://localhost:8000")
    assert card.documentation_url == "https://docs.example.com"


@pytest.mark.skip(
    reason="v1.0 AgentCard moved securitySchemes under supportedInterfaces[*]; "
    "dual-card work will restore v0.3 shape assertions"
)
async def test_build_card_with_security_schemes():
    scheme = HTTPAuthSecurityScheme(type="http", scheme="bearer")
    config = AgentCardConfig(
        name="S",
        description="d",
        security_schemes={"bearer": scheme},
    )
    card = build_agent_card(config, "http://localhost:8000")
    assert card.security_schemes is not None
    assert "bearer" in card.security_schemes


@pytest.mark.skip(
    reason="v1.0 AgentCard does not expose top-level security; moved to supportedInterfaces"
)
async def test_build_card_with_security():
    config = AgentCardConfig(
        name="S",
        description="d",
        security=[{"bearer": []}],
    )
    card = build_agent_card(config, "http://localhost:8000")
    assert card.security == [{"bearer": []}]


async def test_build_card_with_signatures():
    sig = SignatureConfig(protected="eyJhbGciOiJSUzI1NiJ9", signature="abc123")
    config = AgentCardConfig(name="Sig", description="d", signatures=[sig])
    card = build_agent_card(config, "http://localhost:8000")
    assert card.signatures is not None
    assert len(card.signatures) == 1
    assert card.signatures[0].protected == "eyJhbGciOiJSUzI1NiJ9"
    assert card.signatures[0].signature == "abc123"
    assert card.signatures[0].header is None


async def test_build_card_with_signatures_and_header():
    sig = SignatureConfig(
        protected="eyJhbGciOiJSUzI1NiJ9",
        signature="abc123",
        header={"kid": "key-1"},
    )
    config = AgentCardConfig(name="Sig", description="d", signatures=[sig])
    card = build_agent_card(config, "http://localhost:8000")
    assert card.signatures is not None
    # v10 header is a proto Struct; compare as dict
    assert dict(card.signatures[0].header) == {"kid": "key-1"}


@pytest.mark.skip(
    reason="v1.0 AgentCard relocates security/securitySchemes under supportedInterfaces[*]; "
    "dual-card work will restore v0.3 shape assertions"
)
async def test_build_card_all_new_fields():
    config = AgentCardConfig(
        name="Full",
        description="All fields",
        provider=ProviderConfig(organization="Acme", url="https://acme.example.com"),
        icon_url="https://example.com/icon.png",
        documentation_url="https://docs.example.com",
        security_schemes={
            "bearer": HTTPAuthSecurityScheme(type="http", scheme="bearer"),
        },
        security=[{"bearer": []}],
        signatures=[
            SignatureConfig(protected="eyJhbGciOiJSUzI1NiJ9", signature="abc123"),
        ],
        skills=[
            SkillConfig(
                id="s1",
                name="Skill",
                description="d",
                input_modes=["text/plain"],
                output_modes=["application/json"],
                security=[{"bearer": []}],
            ),
        ],
    )
    card = build_agent_card(config, "http://localhost:8000")
    assert card.provider is not None
    assert card.icon_url == "https://example.com/icon.png"
    assert card.documentation_url == "https://docs.example.com"
    assert card.security_schemes is not None
    assert card.security is not None
    assert card.signatures is not None
    assert card.skills[0].input_modes == ["text/plain"]
    assert card.skills[0].output_modes == ["application/json"]
    assert card.skills[0].security == [{"bearer": []}]


async def test_v10_card_translates_common_security_schemes():
    """apiKey and http bearer schemes translate to the v1.0 card shape."""
    from a2a_pydantic import v03

    config = AgentCardConfig(
        name="S",
        description="d",
        security_schemes={
            "bearer": v03.SecurityScheme(
                root=v03.HTTPAuthSecurityScheme(type="http", scheme="bearer")
            ),
            "apikey": v03.SecurityScheme(
                root=v03.APIKeySecurityScheme.model_validate(
                    {"type": "apiKey", "name": "X-Key", "in": "header"}
                )
            ),
        },
        security=[{"bearer": []}],
    )
    card = build_agent_card(config, "http://localhost:8000", protocol_version="1.0")
    assert set(card.security_schemes) == {"bearer", "apikey"}
    assert card.security_schemes["bearer"].http_auth_security_scheme.scheme == "bearer"
    assert card.security_schemes["apikey"].api_key_security_scheme.name == "X-Key"
    assert card.security_schemes["apikey"].api_key_security_scheme.location == "header"
    assert len(card.security_requirements) == 1


async def test_v10_card_drops_requirements_for_untranslated_schemes(caplog):
    """Requirements referencing untranslatable/undeclared schemes are dropped
    so the card never advertises requirements pointing at missing schemes."""
    import logging

    config = AgentCardConfig(
        name="S",
        description="d",
        # No security_schemes at all — the requirement references nothing.
        security=[{"oauth": ["read"]}],
    )
    with caplog.at_level(logging.WARNING, logger="a2akit.agent_card"):
        card = build_agent_card(config, "http://localhost:8000", protocol_version="1.0")
    assert card.security_requirements == []
    assert card.security_schemes == {}
    assert any("dropped security" in r.message for r in caplog.records)


async def test_build_card_defaults_unchanged():
    """AgentCardConfig without new fields behaves exactly as before."""
    config = AgentCardConfig(name="Default", description="d")
    card = build_agent_card(config, "http://localhost:8000")
    assert card.provider is None
    # v10 replaces Nones with empty strings / lists; check falsy
    assert not card.icon_url
    assert not card.documentation_url
    assert not card.signatures


async def test_skill_with_input_output_modes():
    config = AgentCardConfig(
        name="M",
        description="d",
        skills=[
            SkillConfig(
                id="s1",
                name="S",
                description="d",
                input_modes=["text/plain", "application/json"],
                output_modes=["text/plain"],
            ),
        ],
    )
    card = build_agent_card(config, "http://localhost:8000")
    skill = card.skills[0]
    assert skill.input_modes == ["text/plain", "application/json"]
    assert skill.output_modes == ["text/plain"]


async def test_skill_without_modes_inherits_defaults():
    config = AgentCardConfig(
        name="M",
        description="d",
        skills=[SkillConfig(id="s1", name="S", description="d")],
    )
    card = build_agent_card(config, "http://localhost:8000")
    skill = card.skills[0]
    # v10 replaces Nones with empty lists
    assert not skill.input_modes
    assert not skill.output_modes


@pytest.mark.skip(
    reason="v1.0 AgentSkill renames security -> security_requirements; "
    "dual-card work will restore v0.3 shape assertions"
)
async def test_skill_with_security():
    config = AgentCardConfig(
        name="M",
        description="d",
        skills=[
            SkillConfig(
                id="s1",
                name="S",
                description="d",
                security=[{"api_key": ["read"]}],
            ),
        ],
    )
    card = build_agent_card(config, "http://localhost:8000")
    assert card.skills[0].security == [{"api_key": ["read"]}]
