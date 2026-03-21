"""AgentCard configuration and builder utilities."""

from __future__ import annotations

from typing import Any, Literal

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentCardSignature,
    AgentExtension,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    SecurityScheme,
    TransportProtocol,
)
from pydantic import BaseModel, Field


class ProviderConfig(BaseModel):
    """A2A §5.5.1 — AgentProvider information."""

    organization: str
    url: str


class SignatureConfig(BaseModel):
    """A2A §5.5.6 — AgentCardSignature (JWS per RFC 7515).

    a2akit does NOT compute signatures. The user generates JWS externally
    and passes the finished values here. Validation is the client's job.
    """

    protected: str
    signature: str
    header: dict[str, Any] | None = None


class SkillConfig(BaseModel):
    """User-friendly skill definition without A2A protocol imports."""

    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)

    input_modes: list[str] | None = None
    output_modes: list[str] | None = None
    security: list[dict[str, list[str]]] | None = None


class ExtensionConfig(BaseModel):
    """User-friendly extension definition without A2A protocol imports.

    Example::

        ExtensionConfig(
            uri="urn:example:custom-logging",
            description="Custom structured logging extension",
            required=True,
            params={"log_level": "debug"},
        )
    """

    uri: str
    description: str | None = None
    required: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class CapabilitiesConfig(BaseModel):
    """Declares which A2A protocol features this agent supports.

    All capabilities default to False (opt-in). Supported features:
    streaming, push_notifications, state_transition_history,
    extended_agent_card, extensions.
    """

    streaming: bool = False
    push_notifications: bool = False
    state_transition_history: bool = False
    extended_agent_card: bool = False
    extensions: list[AgentExtension] | None = None


class AgentCardConfig(BaseModel):
    """User-friendly configuration for building an AgentCard."""

    name: str
    description: str
    version: str = "1.0.0"
    protocol_version: str = "0.3.0"
    skills: list[SkillConfig] = Field(default_factory=list)
    extensions: list[ExtensionConfig] = Field(default_factory=list)

    capabilities: CapabilitiesConfig = Field(default_factory=CapabilitiesConfig)

    protocol: Literal["jsonrpc", "http+json"] = "jsonrpc"

    supports_authenticated_extended_card: bool = False

    input_modes: list[str] = Field(default_factory=lambda: ["application/json", "text/plain"])
    output_modes: list[str] = Field(default_factory=lambda: ["application/json", "text/plain"])

    # P1
    provider: ProviderConfig | None = None
    security_schemes: dict[str, SecurityScheme] | None = None
    security: list[dict[str, list[str]]] | None = None

    # P2
    icon_url: str | None = None
    documentation_url: str | None = None

    # P3
    signatures: list[SignatureConfig] | None = None


def _to_agent_skill(skill: SkillConfig) -> AgentSkill:
    """Convert a SkillConfig to the A2A AgentSkill type."""
    return AgentSkill(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        tags=skill.tags,
        examples=skill.examples or None,
        input_modes=skill.input_modes,
        output_modes=skill.output_modes,
        security=skill.security,
    )


def _to_agent_extension(ext: ExtensionConfig) -> AgentExtension:
    """Convert an ExtensionConfig to the A2A AgentExtension type."""
    return AgentExtension(
        uri=ext.uri,
        description=ext.description,
        required=ext.required or None,  # omit False from serialization
        params=ext.params or None,
    )


def validate_protocol(protocol: str) -> str:
    """Validate the protocol string, raising ValueError for unsupported values."""
    supported = ["http+json", "jsonrpc"]
    if protocol == "grpc":
        msg = f"Protocol 'grpc' is not yet supported by a2akit. Supported protocols: {supported}"
        raise ValueError(msg)
    if protocol not in supported:
        msg = f"Unknown protocol {protocol!r}. Supported protocols: {supported}"
        raise ValueError(msg)
    return protocol


def build_agent_card(config: AgentCardConfig, base_url: str) -> AgentCard:
    """Build a full AgentCard from user config and the runtime base URL."""
    stripped = base_url.rstrip("/")
    if config.protocol == "jsonrpc":
        agent_url = stripped
        transport = TransportProtocol.jsonrpc
    else:
        agent_url = f"{stripped}/v1"
        transport = TransportProtocol.http_json

    caps = config.capabilities
    return AgentCard(
        protocol_version=config.protocol_version,
        name=config.name,
        description=config.description,
        url=agent_url,
        preferred_transport=transport,
        additional_interfaces=[
            AgentInterface(url=agent_url, transport=transport),
        ],
        version=config.version,
        capabilities=AgentCapabilities(
            extensions=[_to_agent_extension(e) for e in config.extensions] or None,
            streaming=caps.streaming,
            push_notifications=caps.push_notifications,
            state_transition_history=caps.state_transition_history,
        ),
        default_input_modes=config.input_modes,
        default_output_modes=config.output_modes,
        skills=[_to_agent_skill(s) for s in config.skills],
        supports_authenticated_extended_card=config.supports_authenticated_extended_card,
        provider=AgentProvider(
            organization=config.provider.organization,
            url=config.provider.url,
        )
        if config.provider
        else None,
        icon_url=config.icon_url,
        documentation_url=config.documentation_url,
        security_schemes=config.security_schemes,
        security=config.security,
        signatures=[
            AgentCardSignature(
                protected=s.protected,
                signature=s.signature,
                header=s.header,
            )
            for s in config.signatures
        ]
        if config.signatures
        else None,
    )


def external_base_url(headers: dict[str, str], scheme: str, netloc: str) -> str:
    """Derive the external base URL from request headers (proxy-aware)."""
    resolved_scheme = (headers.get("x-forwarded-proto") or scheme).split(",")[0].strip()
    resolved_host = (
        (headers.get("x-forwarded-host") or headers.get("host") or netloc).split(",")[0].strip()
    )
    return f"{resolved_scheme}://{resolved_host}"
