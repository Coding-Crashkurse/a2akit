"""AgentCard configuration and builder utilities."""

from __future__ import annotations

from typing import Any

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentExtension,
    AgentInterface,
    AgentSkill,
    TransportProtocol,
)
from pydantic import BaseModel, Field


class SkillConfig(BaseModel):
    """User-friendly skill definition without A2A protocol imports."""

    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)


class ExtensionConfig(BaseModel):
    """User-friendly extension definition without A2A protocol imports."""

    uri: str
    description: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class AgentCardConfig(BaseModel):
    """User-friendly configuration for building an AgentCard."""

    name: str
    description: str
    version: str = "1.0.0"
    protocol_version: str = "0.3.0"
    skills: list[SkillConfig] = Field(default_factory=list)
    extensions: list[ExtensionConfig] = Field(default_factory=list)

    streaming: bool = True
    push_notifications: bool = False
    supports_extended_card: bool = False

    input_modes: list[str] = Field(
        default_factory=lambda: ["application/json", "text/plain"]
    )
    output_modes: list[str] = Field(
        default_factory=lambda: ["application/json", "text/plain"]
    )


def _to_agent_skill(skill: SkillConfig) -> AgentSkill:
    """Convert a SkillConfig to the A2A AgentSkill type."""
    return AgentSkill(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        tags=skill.tags or None,
        examples=skill.examples or None,
    )


def _to_agent_extension(ext: ExtensionConfig) -> AgentExtension:
    """Convert an ExtensionConfig to the A2A AgentExtension type."""
    return AgentExtension(
        uri=ext.uri,
        description=ext.description,
        params=ext.params or None,
    )


def build_agent_card(config: AgentCardConfig, base_url: str) -> AgentCard:
    """Build a full AgentCard from user config and the runtime base URL."""
    return AgentCard(
        protocol_version=config.protocol_version,
        name=config.name,
        description=config.description,
        url=base_url,
        preferred_transport=TransportProtocol.http_json,
        additional_interfaces=[
            AgentInterface(url=base_url, transport=TransportProtocol.http_json),
        ],
        version=config.version,
        capabilities=AgentCapabilities(
            extensions=[_to_agent_extension(e) for e in config.extensions] or None,
            streaming=config.streaming,
            push_notifications=config.push_notifications,
            state_transition_history=False,
        ),
        default_input_modes=config.input_modes,
        default_output_modes=config.output_modes,
        skills=[_to_agent_skill(s) for s in config.skills],
        supports_authenticated_extended_card=config.supports_extended_card,
    )


def external_base_url(headers: dict, scheme: str, netloc: str) -> str:
    """Derive the external base URL from request headers (proxy-aware)."""
    resolved_scheme = (headers.get("x-forwarded-proto") or scheme).split(",")[0].strip()
    resolved_host = (
        (headers.get("x-forwarded-host") or headers.get("host") or netloc)
        .split(",")[0]
        .strip()
    )
    return f"{resolved_scheme}://{resolved_host}"
