"""Webhook URL validation (anti-SSRF).

Validation resolves the webhook hostname via DNS and rejects URLs whose
addresses are not globally routable. To defeat TOCTOU / DNS-rebinding
attacks (an attacker alternating DNS answers between validation time and
connect time), :func:`resolve_webhook_url` returns the exact IPs the
validation saw and the delivery service pins its TCP connection to one of
them — the original hostname is preserved in the ``Host`` header and, for
HTTPS, in the TLS SNI + certificate hostname check.

No pinning is applied in ``allowed_hosts`` mode (the operator explicitly
trusts those hostnames, and DNS is skipped entirely) or for IP-literal
hosts (inherently pinned). For untrusted multi-tenant deployments prefer
``allowed_hosts`` with hostnames you control.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedWebhookURL:
    """A validated webhook URL plus the addresses validation was based on.

    ``pinned_ips`` is ``None`` when there is nothing to pin: allowlist mode
    (DNS is skipped — the operator trusts the hostname) or an IP-literal
    host (connections are inherently pinned). Otherwise it carries the DNS
    answers observed during validation, in resolution order, so delivery
    can connect to exactly those addresses.
    """

    hostname: str
    pinned_ips: tuple[str, ...] | None = None


@dataclass(frozen=True)
class WebhookValidationPolicy:
    """Active webhook validation settings.

    Published by the delivery service so registration-time validation in
    the push endpoints applies the same rules delivery will later enforce.
    """

    allow_http: bool = False
    allowed_hosts: set[str] | None = None
    blocked_hosts: set[str] | None = None


_registration_policy: WebhookValidationPolicy | None = None


def set_registration_policy(policy: WebhookValidationPolicy | None) -> None:
    """Publish the policy used to validate webhook URLs at registration.

    Called by :class:`~a2akit.push.delivery.WebhookDeliveryService` on
    construction. Process-global: if multiple A2A servers run in one
    process, the most recently constructed delivery service wins.
    """
    global _registration_policy
    _registration_policy = policy


def get_registration_policy() -> WebhookValidationPolicy | None:
    """Return the active registration-time validation policy, if any."""
    return _registration_policy


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check whether an IP address is unsafe to reach from the server.

    Uses Python's own ``is_global`` classification (maintained against the
    IANA special-purpose address registries) rather than a hand-maintained
    allow/deny list. This automatically rejects:

    - Loopback (127.0.0.0/8, ::1)
    - Private (RFC 1918, RFC 4193 ULA)
    - Link-local (169.254.0.0/16, fe80::/10)
    - Reserved / unspecified (notably ``0.0.0.0``, which Linux/macOS
      silently route to localhost — a classic SSRF bypass vector)
    - Shared address space, benchmarking, documentation, multicast, etc.

    IPv4-mapped IPv6 addresses (``::ffff:a.b.c.d``) are unwrapped first so
    that an attacker cannot smuggle a private IPv4 through an IPv6 literal.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not ip.is_global


async def _getaddrinfo(hostname: str) -> Sequence[tuple[Any, ...]]:
    """Resolve *hostname* via the event loop's resolver (patchable in tests)."""
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(hostname, None, proto=0)


async def resolve_webhook_url(
    url: str,
    *,
    allow_http: bool = False,
    allowed_hosts: set[str] | None = None,
    blocked_hosts: set[str] | None = None,
) -> ResolvedWebhookURL | None:
    """Validate a webhook URL for safety and return its pinned addresses.

    Checks:
    1. Scheme is https (unless allow_http for dev)
    2. No private/loopback IP addresses (resolved via DNS)
    3. No blocked hostnames
    4. Optional allowlist enforcement

    Returns ``None`` when the URL is unsafe, otherwise a
    :class:`ResolvedWebhookURL` carrying the DNS answers the validation was
    based on (``pinned_ips is None`` when no DNS resolution took place).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None

    if not allow_http and parsed.scheme != "https":
        return None
    if allow_http and parsed.scheme == "http":
        logger.warning(
            "Allowing insecure HTTP webhook URL %r — do NOT use in production (A2A §4.1)",
            url,
        )
    if parsed.scheme not in ("http", "https"):
        return None

    hostname = parsed.hostname
    if not hostname:
        return None

    if blocked_hosts and hostname.lower() in blocked_hosts:
        return None
    if allowed_hosts:
        # Allowlist mode: skip DNS resolution — the operator explicitly trusts these hosts.
        if hostname.lower() in allowed_hosts:
            return ResolvedWebhookURL(hostname=hostname, pinned_ips=None)
        return None

    # Check IP literals directly
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        pass  # Not an IP literal — resolve via DNS below
    else:
        if _is_blocked_ip(ip):
            return None
        return ResolvedWebhookURL(hostname=hostname, pinned_ips=None)

    # Async DNS resolution to prevent SSRF via hostname → private IP.
    # Uses the event-loop's getaddrinfo to avoid blocking the loop.
    try:
        addrinfo = await _getaddrinfo(hostname)
    except OSError:
        logger.warning("DNS resolution failed for webhook host %r", hostname)
        return None

    ips: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if _is_blocked_ip(ip):
            logger.warning(
                "Webhook host %r resolves to blocked IP %s (SSRF protection)",
                hostname,
                ip,
            )
            return None
        if str(ip) not in ips:
            ips.append(str(ip))

    if not ips:
        logger.warning("DNS resolution returned no addresses for webhook host %r", hostname)
        return None

    return ResolvedWebhookURL(hostname=hostname, pinned_ips=tuple(ips))


async def validate_webhook_url(
    url: str,
    *,
    allow_http: bool = False,
    allowed_hosts: set[str] | None = None,
    blocked_hosts: set[str] | None = None,
) -> bool:
    """Validate a webhook URL for safety (see :func:`resolve_webhook_url`)."""
    resolved = await resolve_webhook_url(
        url,
        allow_http=allow_http,
        allowed_hosts=allowed_hosts,
        blocked_hosts=blocked_hosts,
    )
    return resolved is not None
