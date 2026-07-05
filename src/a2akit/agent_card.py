"""AgentCard configuration and builder utilities.

Exposes a version-agnostic :class:`AgentCardConfig` plus two builders:

- :func:`build_agent_card_v03` — renders the v0.3 wire shape.
- :func:`build_agent_card_v10` — renders the v1.0 wire shape with
  ``supported_interfaces[]`` instead of ``url`` / ``preferred_transport``.

The dispatcher :func:`build_agent_card` picks one based on the requested
protocol version (defaults to v1.0 per spec §6).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from a2a_pydantic import v03, v10
from pydantic import BaseModel, Field

from a2akit._protocol import ProtocolVersion

logger = logging.getLogger(__name__)


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
    """User-friendly extension definition without A2A protocol imports."""

    uri: str
    description: str | None = None
    required: bool = False
    params: dict[str, Any] = Field(default_factory=dict)


class CapabilitiesConfig(BaseModel):
    """Declares which A2A protocol features this agent supports.

    All capabilities default to False (opt-in). ``extensions`` accepts raw
    v03 ``AgentExtension`` objects for back-compat; ``ExtensionConfig``
    objects on ``AgentCardConfig.extensions`` are the newer idiomatic path.
    """

    streaming: bool = False
    push_notifications: bool = False
    state_transition_history: bool = False
    extended_agent_card: bool = False
    extensions: list[v03.AgentExtension] | None = None


class AgentCardConfig(BaseModel):
    """User-friendly configuration for building an AgentCard (version-neutral)."""

    name: str
    description: str
    version: str = "1.0.0"
    # Wire-level protocol version advertised on the card. For v0.3 cards this
    # ends up as the top-level ``protocolVersion``; for v1.0 cards it is
    # carried per-interface inside ``supportedInterfaces[]``.
    protocol_version: str = "0.3.0"
    skills: list[SkillConfig] = Field(default_factory=list)
    extensions: list[ExtensionConfig] = Field(default_factory=list)

    capabilities: CapabilitiesConfig = Field(default_factory=CapabilitiesConfig)

    protocol: Literal["jsonrpc", "http+json"] = "jsonrpc"

    supports_authenticated_extended_card: bool = False

    input_modes: list[str] = Field(default_factory=lambda: ["application/json", "text/plain"])
    output_modes: list[str] = Field(default_factory=lambda: ["application/json", "text/plain"])

    provider: ProviderConfig | None = None
    security_schemes: dict[str, v03.SecurityScheme] | None = None
    security: list[dict[str, list[str]]] | None = None

    icon_url: str | None = None
    documentation_url: str | None = None

    signatures: list[SignatureConfig] | None = None


def _to_v03_agent_skill(skill: SkillConfig) -> v03.AgentSkill:
    return v03.AgentSkill(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        tags=skill.tags,
        examples=skill.examples or None,
        input_modes=skill.input_modes,
        output_modes=skill.output_modes,
        security=skill.security,
    )


def _to_v03_extension(ext: ExtensionConfig) -> v03.AgentExtension:
    return v03.AgentExtension(
        uri=ext.uri,
        description=ext.description,
        required=ext.required or None,
        params=ext.params or None,
    )


def validate_protocol(protocol: str) -> str:
    """Validate the ``protocol`` binding, raising for unsupported values."""
    supported = ["http+json", "jsonrpc"]
    if protocol == "grpc":
        msg = f"Protocol 'grpc' is not yet supported by a2akit. Supported protocols: {supported}"
        raise ValueError(msg)
    if protocol not in supported:
        msg = f"Unknown protocol {protocol!r}. Supported protocols: {supported}"
        raise ValueError(msg)
    return protocol


def build_agent_card_v03(
    config: AgentCardConfig,
    base_url: str,
    additional_protocols: list[str] | None = None,
) -> v03.AgentCard:
    """Build a v0.3 AgentCard."""
    stripped = base_url.rstrip("/")
    if config.protocol == "jsonrpc":
        agent_url = stripped
        transport = v03.TransportProtocol.jsonrpc
    else:
        agent_url = f"{stripped}/v1"
        transport = v03.TransportProtocol.http_json

    additional_interfaces: list[v03.AgentInterface] = []
    for proto in additional_protocols or []:
        normalized = proto.lower().replace(" ", "")
        if normalized == "jsonrpc" and transport != v03.TransportProtocol.jsonrpc:
            additional_interfaces.append(
                v03.AgentInterface(url=stripped, transport=v03.TransportProtocol.jsonrpc)
            )
        elif (
            normalized in ("http+json", "http", "rest")
            and transport != v03.TransportProtocol.http_json
        ):
            additional_interfaces.append(
                v03.AgentInterface(url=f"{stripped}/v1", transport=v03.TransportProtocol.http_json)
            )

    caps = config.capabilities
    merged_extensions: list[v03.AgentExtension] = [_to_v03_extension(e) for e in config.extensions]
    if caps.extensions:
        merged_extensions.extend(caps.extensions)

    return v03.AgentCard(
        protocol_version=config.protocol_version,
        name=config.name,
        description=config.description,
        url=agent_url,
        preferred_transport=transport,
        additional_interfaces=additional_interfaces or None,
        version=config.version,
        capabilities=v03.AgentCapabilities(
            extensions=merged_extensions or None,
            streaming=caps.streaming,
            push_notifications=caps.push_notifications,
            state_transition_history=caps.state_transition_history,
        ),
        default_input_modes=config.input_modes,
        default_output_modes=config.output_modes,
        skills=[_to_v03_agent_skill(s) for s in config.skills],
        supports_authenticated_extended_card=config.supports_authenticated_extended_card,
        provider=v03.AgentProvider(
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
            v03.AgentCardSignature(
                protected=s.protected,
                signature=s.signature,
                header=s.header,
            )
            for s in config.signatures
        ]
        if config.signatures
        else None,
    )


def _binding_for_protocol(protocol: str) -> str:
    """Map the a2akit protocol shorthand to a v10 ``protocol_binding`` string."""
    normalized = protocol.lower().replace(" ", "")
    if normalized == "jsonrpc":
        return "JSONRPC"
    if normalized in ("http+json", "http", "rest"):
        return "HTTP+JSON"
    if normalized == "grpc":
        return "GRPC"
    raise ValueError(f"Unsupported protocol binding for v1.0 card: {protocol!r}")


def _to_v10_agent_skill(skill: SkillConfig) -> v10.AgentSkill:
    return v10.AgentSkill(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        tags=list(skill.tags),
        examples=list(skill.examples or []),
        input_modes=list(skill.input_modes or []),
        output_modes=list(skill.output_modes or []),
    )


def _to_v10_extension(ext: ExtensionConfig) -> v10.AgentExtension:
    return v10.AgentExtension(
        uri=ext.uri,
        description=ext.description or "",
        required=bool(ext.required),
        params=v10.Struct.model_validate(ext.params) if ext.params else None,
    )


def _to_v10_security_scheme(scheme: v03.SecurityScheme) -> v10.SecurityScheme | None:
    """Translate a v0.3 SecurityScheme to its v1.0 shape.

    Only the common schemes (apiKey, http) translate cleanly; OAuth2,
    OpenID Connect and mTLS need the full v03→v10 converter and return
    ``None`` for now.
    """
    inner = scheme.root
    if isinstance(inner, v03.APIKeySecurityScheme):
        return v10.SecurityScheme(
            api_key_security_scheme=v10.APIKeySecurityScheme(
                description=inner.description or "",
                location=getattr(inner.in_, "value", str(inner.in_)),
                name=inner.name,
            )
        )
    if isinstance(inner, v03.HTTPAuthSecurityScheme):
        return v10.SecurityScheme(
            http_auth_security_scheme=v10.HTTPAuthSecurityScheme(
                bearer_format=inner.bearer_format or "",
                description=inner.description or "",
                scheme=inner.scheme,
            )
        )
    return None


def build_agent_card_v10(
    config: AgentCardConfig,
    base_url: str,
    additional_protocols: list[str] | None = None,
) -> v10.AgentCard:
    """Build a v1.0 AgentCard.

    v1.0 dropped the top-level ``url`` / ``preferred_transport`` /
    ``additional_interfaces`` in favor of a single ``supported_interfaces[]``
    list. Each entry carries its own ``protocol_binding`` and
    ``protocol_version``.
    """
    stripped = base_url.rstrip("/")

    primary_binding = _binding_for_protocol(config.protocol)
    supported: list[v10.AgentInterface] = [
        v10.AgentInterface(
            protocol_binding=primary_binding,
            protocol_version="1.0",
            url=stripped if primary_binding == "JSONRPC" else f"{stripped}",
            tenant="",
        )
    ]
    for proto in additional_protocols or []:
        try:
            binding = _binding_for_protocol(proto)
        except ValueError:
            continue
        if binding == primary_binding:
            continue
        supported.append(
            v10.AgentInterface(
                protocol_binding=binding,
                protocol_version="1.0",
                url=stripped,
                tenant="",
            )
        )

    caps = config.capabilities
    # v1.0 wraps the extension set on AgentCapabilities. Merge both the v0.3
    # compat passthrough (raw objects on caps.extensions) and the newer
    # ExtensionConfig objects on the top-level config.
    merged_extensions: list[v10.AgentExtension] = [_to_v10_extension(e) for e in config.extensions]
    if caps.extensions:
        for ext_v03 in caps.extensions:
            merged_extensions.append(
                v10.AgentExtension(
                    uri=ext_v03.uri,
                    description=ext_v03.description or "",
                    required=bool(ext_v03.required),
                    params=(v10.Struct.model_validate(ext_v03.params) if ext_v03.params else None),
                )
            )

    # Translate the common v0.3 security schemes (apiKey, http) to v1.0.
    # Schemes without a clean translation (OAuth2, OIDC, mTLS) are dropped —
    # together with any security requirement referencing them, so the card
    # never advertises requirements pointing at undeclared schemes.
    schemes_v10: dict[str, v10.SecurityScheme] = {}
    dropped_schemes: list[str] = []
    for scheme_name, scheme in (config.security_schemes or {}).items():
        translated = _to_v10_security_scheme(scheme)
        if translated is None:
            dropped_schemes.append(scheme_name)
        else:
            schemes_v10[scheme_name] = translated

    security_requirements: list[v10.SecurityRequirement] = []
    dropped_requirements = 0
    for entry in config.security or []:
        if any(name not in schemes_v10 for name in entry):
            dropped_requirements += 1
            continue
        requirement_schemes: dict[str, v10.StringList] = {}
        for name, scopes in entry.items():
            requirement_schemes[name] = v10.StringList(strings=list(scopes))
        security_requirements.append(v10.SecurityRequirement(schemes=requirement_schemes))

    if dropped_schemes or dropped_requirements:
        logger.warning(
            "v1.0 agent card: dropped security schemes %s and %d security "
            "requirement(s) referencing untranslated/undeclared schemes",
            dropped_schemes,
            dropped_requirements,
        )

    return v10.AgentCard(
        name=config.name,
        description=config.description,
        version=config.version,
        provider=(
            v10.AgentProvider(
                organization=config.provider.organization,
                url=config.provider.url,
            )
            if config.provider
            else None
        ),
        capabilities=v10.AgentCapabilities(
            streaming=caps.streaming,
            push_notifications=caps.push_notifications,
            extended_agent_card=caps.extended_agent_card
            or config.supports_authenticated_extended_card,
            extensions=merged_extensions or [],
        ),
        default_input_modes=list(config.input_modes),
        default_output_modes=list(config.output_modes),
        supported_interfaces=supported,
        skills=[_to_v10_agent_skill(s) for s in config.skills],
        icon_url=config.icon_url or "",
        documentation_url=config.documentation_url or "",
        security_requirements=security_requirements,
        security_schemes=schemes_v10,
        signatures=[
            v10.AgentCardSignature(
                protected=s.protected,
                signature=s.signature,
                header=(v10.Struct.model_validate(s.header) if s.header else None),
            )
            for s in (config.signatures or [])
        ],
    )


def build_agent_card(
    config: AgentCardConfig,
    base_url: str,
    additional_protocols: list[str] | None = None,
    *,
    protocol_version: ProtocolVersion | str | None = None,
) -> v03.AgentCard | v10.AgentCard:
    """Build an AgentCard for the configured wire version.

    ``protocol_version`` is a single :class:`ProtocolVersion` (or string
    form). ``None`` defaults to v1.0 (spec §6).
    """
    version = (
        protocol_version
        if isinstance(protocol_version, ProtocolVersion)
        else ProtocolVersion.parse(protocol_version)
    )
    if version == ProtocolVersion.V1_0:
        return build_agent_card_v10(config, base_url, additional_protocols)
    return build_agent_card_v03(config, base_url, additional_protocols)


def external_base_url(headers: dict[str, str], scheme: str, netloc: str) -> str:
    """Derive the external base URL from request headers.

    ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` are honored only when
    ``Settings.trust_proxy_headers`` (env ``A2AKIT_TRUST_PROXY_HEADERS``)
    is enabled — trusting them unconditionally would let any client spoof
    the URLs advertised on the agent card.
    """
    from a2akit.config import get_settings

    if get_settings().trust_proxy_headers:
        resolved_scheme = (headers.get("x-forwarded-proto") or scheme).split(",")[0].strip()
        resolved_host = (
            (headers.get("x-forwarded-host") or headers.get("host") or netloc)
            .split(",")[0]
            .strip()
        )
    else:
        resolved_scheme = scheme
        resolved_host = (headers.get("host") or netloc).split(",")[0].strip()
    return f"{resolved_scheme}://{resolved_host}"


__all__ = [
    "AgentCardConfig",
    "CapabilitiesConfig",
    "ExtensionConfig",
    "ProviderConfig",
    "SignatureConfig",
    "SkillConfig",
    "build_agent_card",
    "build_agent_card_v03",
    "build_agent_card_v10",
    "external_base_url",
    "validate_protocol",
]
