"""Webhook URL validation (anti-SSRF)."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_BLOCKED_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check whether an IP address falls into a blocked (private/loopback) range."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in blocked for blocked in _BLOCKED_RANGES)


async def validate_webhook_url(
    url: str,
    *,
    allow_http: bool = False,
    allowed_hosts: set[str] | None = None,
    blocked_hosts: set[str] | None = None,
) -> bool:
    """Validate a webhook URL for safety.

    Checks:
    1. Scheme is https (unless allow_http for dev)
    2. No private/loopback IP addresses (resolved via DNS)
    3. No blocked hostnames
    4. Optional allowlist enforcement
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if not allow_http and parsed.scheme != "https":
        return False
    if allow_http and parsed.scheme == "http":
        logger.warning(
            "Allowing insecure HTTP webhook URL %r — do NOT use in production (A2A §4.1)",
            url,
        )
    if parsed.scheme not in ("http", "https"):
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    if blocked_hosts and hostname.lower() in blocked_hosts:
        return False
    if allowed_hosts:
        # Allowlist mode: skip DNS resolution — the operator explicitly trusts these hosts.
        return hostname.lower() in allowed_hosts

    # Check IP literals directly
    try:
        ip = ipaddress.ip_address(hostname)
        return not _is_blocked_ip(ip)
    except ValueError:
        pass  # Not an IP literal — resolve via DNS below

    # Async DNS resolution to prevent SSRF via hostname → private IP.
    # Uses the event-loop's getaddrinfo to avoid blocking the loop.
    loop = asyncio.get_running_loop()
    try:
        addrinfo = await loop.getaddrinfo(hostname, None, proto=0)
    except OSError:
        logger.warning("DNS resolution failed for webhook host %r", hostname)
        return False

    for _family, _type, _proto, _canonname, sockaddr in addrinfo:
        ip = ipaddress.ip_address(sockaddr[0])
        if _is_blocked_ip(ip):
            logger.warning(
                "Webhook host %r resolves to blocked IP %s (SSRF protection)",
                hostname,
                ip,
            )
            return False

    return True
