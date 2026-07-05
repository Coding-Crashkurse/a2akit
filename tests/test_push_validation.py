"""Tests for webhook URL validation (anti-SSRF)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from a2akit.push.validation import resolve_webhook_url, validate_webhook_url

# Mock DNS resolution for tests — returns a public IP for any hostname.
_PUBLIC_ADDRINFO = [(2, 1, 6, "", ("93.184.216.34", 0))]


def _make_loop_mock(addrinfo):
    """Create a mock event loop whose getaddrinfo returns *addrinfo*."""
    loop = AsyncMock()
    loop.getaddrinfo = AsyncMock(return_value=addrinfo)
    return loop


def _loop_public():
    return _make_loop_mock(_PUBLIC_ADDRINFO)


def _loop_private():
    return _make_loop_mock([(2, 1, 6, "", ("127.0.0.1", 0))])


def _loop_fail():
    loop = AsyncMock()
    loop.getaddrinfo = AsyncMock(side_effect=OSError("DNS resolution failed"))
    return loop


@patch("a2akit.push.validation.asyncio.get_running_loop", _loop_public)
async def test_valid_https_url():
    assert await validate_webhook_url("https://example.com/webhook") is True


async def test_http_rejected_by_default():
    assert await validate_webhook_url("http://example.com/webhook") is False


@patch("a2akit.push.validation.asyncio.get_running_loop", _loop_public)
async def test_http_allowed_in_dev_mode():
    assert await validate_webhook_url("http://example.com/webhook", allow_http=True) is True


async def test_private_ip_10_x():
    assert await validate_webhook_url("https://10.0.0.1/webhook") is False


async def test_private_ip_172_16_x():
    assert await validate_webhook_url("https://172.16.0.1/webhook") is False


async def test_private_ip_192_168_x():
    assert await validate_webhook_url("https://192.168.1.1/webhook") is False


async def test_loopback_127_0_0_1():
    assert await validate_webhook_url("https://127.0.0.1/webhook") is False


async def test_loopback_ipv6():
    assert await validate_webhook_url("https://[::1]/webhook") is False


async def test_link_local_169_254():
    assert await validate_webhook_url("https://169.254.1.1/webhook") is False


async def test_public_ip():
    assert await validate_webhook_url("https://93.184.216.34/webhook") is True


@patch("a2akit.push.validation.asyncio.get_running_loop", _loop_public)
async def test_hostname():
    assert await validate_webhook_url("https://webhook.example.com/path") is True


async def test_no_scheme():
    assert await validate_webhook_url("example.com/webhook") is False


async def test_ftp_scheme():
    assert await validate_webhook_url("ftp://example.com/webhook") is False


async def test_empty_url():
    assert await validate_webhook_url("") is False


async def test_allowed_hosts_match():
    assert (
        await validate_webhook_url("https://allowed.com/webhook", allowed_hosts={"allowed.com"})
        is True
    )


async def test_allowed_hosts_no_match():
    assert (
        await validate_webhook_url("https://other.com/webhook", allowed_hosts={"allowed.com"})
        is False
    )


async def test_blocked_hosts_match():
    assert (
        await validate_webhook_url("https://blocked.com/webhook", blocked_hosts={"blocked.com"})
        is False
    )


async def test_private_ip_allowed_with_http():
    """Private IPs should still be blocked even with allow_http=True."""
    assert await validate_webhook_url("http://10.0.0.1/webhook", allow_http=True) is False


@patch("a2akit.push.validation.asyncio.get_running_loop", _loop_private)
async def test_ssrf_hostname_resolves_to_private_ip():
    """Hostname that resolves to a private IP must be blocked (SSRF)."""
    assert await validate_webhook_url("https://evil.attacker.com/webhook") is False


@patch("a2akit.push.validation.asyncio.get_running_loop", _loop_fail)
async def test_dns_resolution_failure_rejects():
    """Unresolvable hostnames must be rejected."""
    assert await validate_webhook_url("https://nonexistent.invalid/webhook") is False


async def test_unspecified_0_0_0_0_blocked():
    """0.0.0.0 must be blocked — Linux/macOS silently route it to localhost,
    which is a classic SSRF bypass vector for hand-maintained deny lists."""
    assert await validate_webhook_url("https://0.0.0.0/webhook") is False


async def test_unspecified_ipv6_blocked():
    """The IPv6 unspecified address :: is equally unsafe."""
    assert await validate_webhook_url("https://[::]/webhook") is False


async def test_ipv4_mapped_ipv6_private_blocked():
    """::ffff:127.0.0.1 must NOT smuggle a loopback IPv4 through an IPv6 literal."""
    assert await validate_webhook_url("https://[::ffff:127.0.0.1]/webhook") is False


async def test_shared_address_space_blocked():
    """100.64.0.0/10 (CGNAT / shared address space) is not globally routable."""
    assert await validate_webhook_url("https://100.64.0.1/webhook") is False


async def test_documentation_range_blocked():
    """192.0.2.0/24 (TEST-NET-1) is reserved for documentation and not routable."""
    assert await validate_webhook_url("https://192.0.2.1/webhook") is False


@patch("a2akit.push.validation.asyncio.get_running_loop", _loop_public)
async def test_resolve_returns_pinned_ips_for_hostname():
    """DNS-resolved hostnames return the validated IPs for connection pinning."""
    resolved = await resolve_webhook_url("https://example.com/webhook")
    assert resolved is not None
    assert resolved.hostname == "example.com"
    assert resolved.pinned_ips == ("93.184.216.34",)


async def test_resolve_dedupes_pinned_ips(monkeypatch):
    """Duplicate DNS answers (multiple socktypes per IP) are deduplicated."""

    async def _dupes(hostname):
        return [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 2, 17, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("93.184.216.35", 0)),
        ]

    monkeypatch.setattr("a2akit.push.validation._getaddrinfo", _dupes)
    resolved = await resolve_webhook_url("https://example.com/webhook")
    assert resolved is not None
    assert resolved.pinned_ips == ("93.184.216.34", "93.184.216.35")


async def test_resolve_ip_literal_has_no_pinned_ips():
    """IP-literal hosts are inherently pinned — no rewrite needed."""
    resolved = await resolve_webhook_url("https://93.184.216.34/webhook")
    assert resolved is not None
    assert resolved.pinned_ips is None


async def test_resolve_allowlist_has_no_pinned_ips():
    """Allowlist mode skips DNS entirely, so nothing is pinned."""
    resolved = await resolve_webhook_url(
        "https://allowed.com/webhook", allowed_hosts={"allowed.com"}
    )
    assert resolved is not None
    assert resolved.pinned_ips is None


@patch("a2akit.push.validation.asyncio.get_running_loop", _loop_private)
async def test_resolve_private_dns_answer_returns_none():
    assert await resolve_webhook_url("https://evil.attacker.com/webhook") is None


async def test_resolve_empty_dns_answer_returns_none(monkeypatch):
    """An empty (but non-erroring) DNS answer must be rejected, not pinned to nothing."""

    async def _empty(hostname):
        return []

    monkeypatch.setattr("a2akit.push.validation._getaddrinfo", _empty)
    assert await resolve_webhook_url("https://example.com/webhook") is None
