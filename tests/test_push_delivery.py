"""Tests for WebhookDeliveryService."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest
from a2a.types import Task, TaskState, TaskStatus

from a2akit.push.delivery import WebhookDeliveryService, _build_headers
from a2akit.push.models import (
    PushNotificationAuthenticationInfo,
    PushNotificationConfig,
    TaskPushNotificationConfig,
)

_PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """Deterministic DNS: every hostname resolves to a public IP.

    Keeps validation + connection pinning hermetic — no real DNS in tests.
    """

    async def _fake_getaddrinfo(hostname):
        return [(2, 1, 6, "", (_PUBLIC_IP, 0))]

    monkeypatch.setattr("a2akit.push.validation._getaddrinfo", _fake_getaddrinfo)


def _make_task(task_id: str = "task-1", state: str = "working") -> Task:
    return Task(
        id=task_id,
        context_id="ctx-1",
        kind="task",
        status=TaskStatus(state=TaskState(state), timestamp="2026-01-01T00:00:00Z"),
    )


def _make_config(
    task_id: str = "task-1",
    config_id: str = "cfg-1",
    url: str = "https://example.com/webhook",
    token: str | None = None,
) -> TaskPushNotificationConfig:
    return TaskPushNotificationConfig(
        task_id=task_id,
        push_notification_config=PushNotificationConfig(id=config_id, url=url, token=token),
    )


async def test_delivery_success():
    service = WebhookDeliveryService(max_retries=1, allow_http=True, timeout=5.0)
    await service.startup()

    mock_response = httpx.Response(200)
    with patch.object(service._http_client, "post", return_value=mock_response) as mock_post:
        config = _make_config(url="http://example.com/webhook")
        task = _make_task()
        await service.deliver([config], task)
        # Wait for async queue processing
        await asyncio.sleep(0.1)
        mock_post.assert_called_once()

    await service.shutdown()


async def test_delivery_no_retry_on_400():
    service = WebhookDeliveryService(max_retries=3, allow_http=True, timeout=5.0)
    await service.startup()

    mock_response = httpx.Response(400)
    with patch.object(service._http_client, "post", return_value=mock_response) as mock_post:
        config = _make_config(url="http://example.com/webhook")
        task = _make_task()
        await service.deliver([config], task)
        await asyncio.sleep(0.1)
        # Should not retry on 4xx
        assert mock_post.call_count == 1

    await service.shutdown()


async def test_delivery_rejects_invalid_url():
    service = WebhookDeliveryService(max_retries=1, allow_http=False, timeout=5.0)
    await service.startup()

    with patch.object(service._http_client, "post") as mock_post:
        # HTTP URL rejected in production mode
        config = _make_config(url="http://example.com/webhook")
        task = _make_task()
        await service.deliver([config], task)
        await asyncio.sleep(0.1)
        mock_post.assert_not_called()

    await service.shutdown()


async def test_delivery_strips_internal_metadata():
    """Webhook payload MUST NOT leak framework-internal metadata keys
    (``_idempotency_key``, ``_a2akit_direct_reply``, ...). REST/SSE
    clients get a sanitized Task — webhooks must get the same."""
    service = WebhookDeliveryService(max_retries=1, allow_http=True, timeout=5.0)
    await service.startup()

    task = Task(
        id="task-leak",
        context_id="ctx-1",
        kind="task",
        status=TaskStatus(state=TaskState("working"), timestamp="2026-01-01T00:00:00Z"),
        metadata={
            "_idempotency_key": "super-secret-idem",
            "_a2akit_direct_reply": "msg-123",
            "_a2akit_just_created": True,
            "public_label": "visible",
        },
    )

    captured: dict[str, object] = {}

    async def _fake_post(url, json=None, headers=None, extensions=None):
        captured["json"] = json
        return httpx.Response(200)

    with patch.object(service._http_client, "post", side_effect=_fake_post):
        config = _make_config(task_id="task-leak", url="http://example.com/webhook")
        await service.deliver([config], task)
        await asyncio.sleep(0.1)

    await service.shutdown()

    payload = captured.get("json")
    assert isinstance(payload, dict)
    metadata = payload.get("metadata") or {}
    # Internal keys (prefix _) must be stripped.
    assert "_idempotency_key" not in metadata
    assert "_a2akit_direct_reply" not in metadata
    assert "_a2akit_just_created" not in metadata
    # Public keys must survive.
    assert metadata.get("public_label") == "visible"


async def test_delivery_does_not_mutate_original_task():
    """Sanitization must not mutate the Task the worker is still holding."""
    service = WebhookDeliveryService(max_retries=1, allow_http=True, timeout=5.0)
    await service.startup()

    original_metadata = {
        "_idempotency_key": "keep-me",
        "public": "ok",
    }
    task = Task(
        id="task-nomutate",
        context_id="ctx-1",
        kind="task",
        status=TaskStatus(state=TaskState("working"), timestamp="2026-01-01T00:00:00Z"),
        metadata=dict(original_metadata),
    )

    with patch.object(service._http_client, "post", return_value=httpx.Response(200)):
        config = _make_config(task_id="task-nomutate", url="http://example.com/webhook")
        await service.deliver([config], task)
        await asyncio.sleep(0.1)

    await service.shutdown()
    # Original still carries the internal key — only the outbound copy was sanitized.
    assert task.metadata == original_metadata


def test_build_headers_basic():
    config = PushNotificationConfig(url="https://example.com")
    headers = _build_headers(config)
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == "a2akit-push/0.1"
    assert "X-A2A-Notification-Token" not in headers
    assert "Authorization" not in headers


def test_build_headers_with_token():
    config = PushNotificationConfig(url="https://example.com", token="secret")
    headers = _build_headers(config)
    assert headers["X-A2A-Notification-Token"] == "secret"


def test_build_headers_with_auth():
    auth = PushNotificationAuthenticationInfo(schemes=["Bearer"], credentials="my-jwt-token")
    config = PushNotificationConfig(url="https://example.com", authentication=auth)
    headers = _build_headers(config)
    assert headers["Authorization"] == "Bearer my-jwt-token"


async def test_startup_shutdown_lifecycle():
    service = WebhookDeliveryService()
    await service.startup()
    assert service._http_client is not None
    await service.shutdown()


async def test_shutdown_cancels_stuck_workers_before_closing_client():
    """Regression: shutdown() used to call ``asyncio.wait(..., timeout=30)``
    which returns on timeout without cancelling the workers. The workers
    then continued to use ``http_client`` concurrently with
    ``http_client.aclose()``, causing races.

    After the fix, workers that exceed the grace period are force-cancelled
    before the HTTP client is closed.
    """
    service = WebhookDeliveryService(
        max_retries=1,
        allow_http=True,
        timeout=5.0,
        shutdown_grace=0.1,
    )
    await service.startup()

    # Inject a worker task that ignores the sentinel and never exits on its own.
    stuck_started = asyncio.Event()

    async def _stuck_worker():
        stuck_started.set()
        await asyncio.sleep(3600)

    key = ("stuck-task", "stuck-cfg")
    service._delivery_queues[key] = asyncio.Queue()
    stuck_task = asyncio.create_task(_stuck_worker())
    service._queue_workers[key] = stuck_task

    await stuck_started.wait()
    await service.shutdown()

    # Worker was force-cancelled and HTTP client closed.
    assert stuck_task.cancelled() or stuck_task.done()


async def test_delivery_pins_connection_to_validated_ip():
    """HTTPS delivery connects to the validated IP; Host header and SNI
    (certificate hostname) keep the original hostname (DNS-rebinding fix)."""
    service = WebhookDeliveryService(max_retries=1, timeout=5.0)
    await service.startup()

    captured: dict[str, object] = {}

    async def _fake_post(url, json=None, headers=None, extensions=None):
        captured["url"] = str(url)
        captured["headers"] = headers
        captured["extensions"] = extensions
        return httpx.Response(200)

    with patch.object(service._http_client, "post", side_effect=_fake_post):
        config = _make_config(url="https://example.com:8443/webhook")
        await service.deliver([config], _make_task())
        await asyncio.sleep(0.1)

    await service.shutdown()

    assert captured["url"] == f"https://{_PUBLIC_IP}:8443/webhook"
    assert captured["headers"]["Host"] == "example.com:8443"
    assert captured["extensions"]["sni_hostname"] == "example.com"


async def test_delivery_pins_http_without_sni():
    """HTTP pinning rewrites the URL and Host header but sets no SNI."""
    service = WebhookDeliveryService(max_retries=1, allow_http=True, timeout=5.0)
    await service.startup()

    captured: dict[str, object] = {}

    async def _fake_post(url, json=None, headers=None, extensions=None):
        captured["url"] = str(url)
        captured["headers"] = headers
        captured["extensions"] = extensions
        return httpx.Response(200)

    with patch.object(service._http_client, "post", side_effect=_fake_post):
        config = _make_config(url="http://example.com/webhook")
        await service.deliver([config], _make_task())
        await asyncio.sleep(0.1)

    await service.shutdown()

    assert captured["url"] == f"http://{_PUBLIC_IP}/webhook"
    assert captured["headers"]["Host"] == "example.com"
    assert "sni_hostname" not in captured["extensions"]


async def test_delivery_dns_rebinding_cannot_redirect(monkeypatch):
    """An attacker flipping DNS answers after validation must not be able to
    steer the POST — the connection is pinned to the first (validated) answer
    and DNS is resolved exactly once per delivery."""
    calls: list[str] = []

    async def _flip_flop(hostname):
        calls.append(hostname)
        if len(calls) == 1:
            return [(2, 1, 6, "", (_PUBLIC_IP, 0))]
        return [(2, 1, 6, "", ("169.254.169.254", 0))]  # rebound to metadata IP

    monkeypatch.setattr("a2akit.push.validation._getaddrinfo", _flip_flop)

    service = WebhookDeliveryService(max_retries=1, timeout=5.0)
    await service.startup()

    captured: dict[str, object] = {}

    async def _fake_post(url, json=None, headers=None, extensions=None):
        captured["url"] = str(url)
        return httpx.Response(200)

    with patch.object(service._http_client, "post", side_effect=_fake_post):
        config = _make_config(url="https://rebind.example.com/webhook")
        await service.deliver([config], _make_task())
        await asyncio.sleep(0.1)

    await service.shutdown()

    assert captured["url"] == f"https://{_PUBLIC_IP}/webhook"
    assert calls == ["rebind.example.com"]


async def test_delivery_no_pinning_for_ip_literal_and_allowlist():
    """IP-literal hosts are inherently pinned and allowlisted hosts skip DNS —
    neither gets a rewritten URL or Host override."""
    service = WebhookDeliveryService(
        max_retries=1, timeout=5.0, allowed_hosts={"trusted.example.com"}
    )
    await service.startup()

    captured: list[tuple[str, dict]] = []

    async def _fake_post(url, json=None, headers=None, extensions=None):
        captured.append((str(url), headers))
        return httpx.Response(200)

    with patch.object(service._http_client, "post", side_effect=_fake_post):
        await service.deliver(
            [_make_config(config_id="cfg-allow", url="https://trusted.example.com/hook")],
            _make_task(),
        )
        await asyncio.sleep(0.1)

    await service.shutdown()

    service2 = WebhookDeliveryService(max_retries=1, timeout=5.0)
    await service2.startup()
    with patch.object(service2._http_client, "post", side_effect=_fake_post):
        await service2.deliver(
            [_make_config(config_id="cfg-ip", url=f"https://{_PUBLIC_IP}/hook")],
            _make_task(),
        )
        await asyncio.sleep(0.1)
    await service2.shutdown()

    assert len(captured) == 2
    for _url, headers in captured:
        assert "Host" not in headers
    assert captured[0][0] == "https://trusted.example.com/hook"
    assert captured[1][0] == f"https://{_PUBLIC_IP}/hook"


async def test_delivery_retries_5xx_with_backoff_until_exhaustion(caplog):
    """5xx responses are retried with exponential backoff up to max_retries,
    then exhaustion is logged."""
    service = WebhookDeliveryService(max_retries=3, retry_base_delay=0.01, timeout=5.0)
    await service.startup()

    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def _fake_sleep(delay):
        sleeps.append(delay)
        await real_sleep(0)

    mock_response = httpx.Response(500)
    with (
        patch.object(service._http_client, "post", return_value=mock_response) as mock_post,
        patch("a2akit.push.delivery.asyncio.sleep", side_effect=_fake_sleep),
    ):
        config = _make_config(url="https://example.com/webhook")
        await service.deliver([config], _make_task())
        for _ in range(200):
            if mock_post.call_count >= 3:
                break
            await real_sleep(0.01)

    await service.shutdown()

    assert mock_post.call_count == 3
    assert sleeps == [0.01, 0.02]  # exponential backoff between attempts
    assert "exhausted all 3 retries" in caplog.text


async def test_back_to_back_transitions_delivered_in_order():
    """Per-config queues deliver back-to-back transitions strictly in order
    (the design property PushDeliveryEmitter relies on)."""
    service = WebhookDeliveryService(max_retries=1, timeout=5.0)
    await service.startup()

    seen: list[str] = []

    async def _fake_post(url, json=None, headers=None, extensions=None):
        await asyncio.sleep(0.01)  # make interleaving possible if ordering broke
        seen.append(json["status"]["state"])
        return httpx.Response(200)

    with patch.object(service._http_client, "post", side_effect=_fake_post):
        config = _make_config(url="https://example.com/webhook")
        for state in ("submitted", "working", "completed"):
            await service.deliver([config], _make_task(state=state))
        for _ in range(200):
            if len(seen) == 3:
                break
            await asyncio.sleep(0.01)

    await service.shutdown()

    assert seen == ["submitted", "working", "completed"]


async def test_queue_bounded_drops_oldest(caplog):
    """A full per-config queue drops the oldest snapshot (with a warning)
    instead of growing without bound."""
    service = WebhookDeliveryService(max_retries=1, timeout=5.0, max_queue_size=2)
    await service.startup()

    entered = asyncio.Event()
    release = asyncio.Event()
    seen: list[int] = []

    async def _fake_post(url, json=None, headers=None, extensions=None):
        entered.set()
        await release.wait()
        seen.append(json["metadata"]["n"])
        return httpx.Response(200)

    def _task_n(n: int) -> Task:
        task = _make_task()
        task.metadata = {"n": n}
        return task

    with patch.object(service._http_client, "post", side_effect=_fake_post):
        config = _make_config(url="https://example.com/webhook")
        await service.deliver([config], _task_n(1))
        # Wait until snapshot 1 is in-flight (worker blocked in POST).
        await asyncio.wait_for(entered.wait(), timeout=2)
        # Queue capacity is 2: snapshots 2+3 fill it, 4 drops 2, 5 drops 3.
        for n in (2, 3, 4, 5):
            await service.deliver([config], _task_n(n))
        release.set()
        for _ in range(200):
            if len(seen) == 3:
                break
            await asyncio.sleep(0.01)

    await service.shutdown()

    assert seen == [1, 4, 5]
    assert "dropping oldest snapshot" in caplog.text


async def test_idle_timeout_cleans_up_queue():
    """Queue workers exit after idle_timeout when no new events arrive."""
    service = WebhookDeliveryService(max_retries=1, allow_http=True, timeout=5.0, idle_timeout=0.2)
    await service.startup()

    mock_response = httpx.Response(200)
    with patch.object(service._http_client, "post", return_value=mock_response):
        config = _make_config(url="http://example.com/webhook")
        task = _make_task()  # state=working, non-terminal
        await service.deliver([config], task)
        await asyncio.sleep(0.05)
        # Worker is alive, queue exists
        assert len(service._queue_workers) == 1

        # Wait for idle timeout to fire
        await asyncio.sleep(0.3)
        # Worker should have exited and cleaned up
        assert len(service._queue_workers) == 0
        assert len(service._delivery_queues) == 0

    await service.shutdown()
