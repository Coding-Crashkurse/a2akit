"""Webhook delivery service - sends task updates to client-provided URLs."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse, urlunparse

import httpx

from a2akit.endpoints import _sanitize_task_for_client
from a2akit.push.validation import (
    WebhookValidationPolicy,
    resolve_webhook_url,
    set_registration_policy,
)

if TYPE_CHECKING:
    from a2a_pydantic import v10

    from a2akit.push.models import PushNotificationConfig, TaskPushNotificationConfig

logger = logging.getLogger(__name__)


class WebhookDeliveryService:
    """Delivers task updates to client-provided webhook URLs.

    Design decisions:
    - Fires on ALL state transitions (not just terminal)
    - Best-effort delivery with configurable retries
    - Exponential backoff between retries
    - Sequential per config (preserves event ordering)
    - Parallel between different configs (fan-out)
    - Non-blocking: delivery failures never affect task processing
    - Failed deliveries are logged, not persisted (no dead-letter queue)
    - Bounded per-config queues with drop-oldest semantics (snapshots
      are best-effort; the newest Task snapshot supersedes older ones)
    - Anti-SSRF: connections are pinned to the IPs seen at validation
      time (defeats DNS rebinding); the validation policy is published
      via ``set_registration_policy`` so config registration rejects
      unsafe URLs with the same rules
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        retry_base_delay: float = 1.0,
        timeout: float = 10.0,
        max_concurrent_deliveries: int = 50,
        allow_http: bool = False,
        allowed_hosts: set[str] | None = None,
        blocked_hosts: set[str] | None = None,
        idle_timeout: float = 300.0,
        shutdown_grace: float = 30.0,
        max_queue_size: int = 100,
    ) -> None:
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrent_deliveries)
        self._allow_http = allow_http
        self._allowed_hosts = allowed_hosts
        self._blocked_hosts = blocked_hosts
        self._idle_timeout = idle_timeout
        self._shutdown_grace = shutdown_grace
        self._max_queue_size = max_queue_size
        self._http_client: httpx.AsyncClient | None = None
        # Per-config delivery queues ensure sequential ordering
        # Key: (task_id, config_id)
        self._delivery_queues: dict[tuple[str, str], asyncio.Queue[v10.Task | None]] = {}
        self._queue_workers: dict[tuple[str, str], asyncio.Task[None]] = {}
        # Let registration-time validation apply the same rules delivery uses.
        set_registration_policy(
            WebhookValidationPolicy(
                allow_http=allow_http,
                allowed_hosts=allowed_hosts,
                blocked_hosts=blocked_hosts,
            )
        )

    async def startup(self) -> None:
        """Initialize the HTTP client."""
        self._http_client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,  # Security: no redirect following
            verify=True,  # TLS verification mandatory
        )

    async def shutdown(self) -> None:
        """Gracefully shut down all delivery workers.

        Workers are signaled via sentinels first.  Any worker that does
        not finish within the grace period is force-cancelled so the
        HTTP client can be closed safely — ``asyncio.wait(timeout=...)``
        alone returns on timeout but leaves tasks running, which would
        race against ``http_client.aclose()`` below.
        """
        for key in list(self._delivery_queues):
            self._enqueue(key, None)  # Sentinel
        if self._queue_workers:
            workers = list(self._queue_workers.values())
            try:
                await asyncio.wait_for(
                    asyncio.gather(*workers, return_exceptions=True),
                    timeout=self._shutdown_grace,
                )
            except TimeoutError:
                logger.warning(
                    "Webhook delivery workers did not finish within %.1fs; cancelling",
                    self._shutdown_grace,
                )
                for w in workers:
                    if not w.done():
                        w.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
        if self._http_client:
            await self._http_client.aclose()

    async def deliver(
        self,
        configs: list[TaskPushNotificationConfig],
        task: v10.Task,
    ) -> None:
        """Fan out delivery to all webhook configs for a task.

        Each config gets its own sequential queue so events arrive
        in order. Different configs are delivered in parallel.
        """
        for config in configs:
            config_id = config.id or "default"
            queue_key = (task.id, config_id)

            existing_worker = self._queue_workers.get(queue_key)
            if existing_worker is None or existing_worker.done():
                # Worker exited (idle timeout / terminal state) or never existed.
                # Clean up stale references and start fresh.
                self._delivery_queues.pop(queue_key, None)
                self._queue_workers.pop(queue_key, None)
                queue: asyncio.Queue[v10.Task | None] = asyncio.Queue(maxsize=self._max_queue_size)
                self._delivery_queues[queue_key] = queue
                worker = asyncio.create_task(self._queue_worker(queue_key, queue, config))
                self._queue_workers[queue_key] = worker
                worker.add_done_callback(
                    lambda fut, k=queue_key: self._cleanup_queue(k, fut)  # type: ignore[misc]
                )

            self._enqueue(queue_key, task)

    def _enqueue(self, key: tuple[str, str], item: v10.Task | None) -> None:
        """Enqueue with drop-oldest semantics.

        Webhooks are best-effort Task snapshots — when a slow webhook
        backs the queue up, the newest snapshot is more valuable than the
        oldest, so the oldest is dropped (with a warning) instead of
        growing the queue without bound.
        """
        queue = self._delivery_queues[key]
        while True:
            try:
                queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - no await between full/get
                    continue
                logger.warning(
                    "Webhook delivery queue %s full (maxsize %d); dropping oldest snapshot",
                    key,
                    self._max_queue_size,
                )

    def _cleanup_queue(self, key: tuple[str, str], finished_worker: asyncio.Task[None]) -> None:
        if self._queue_workers.get(key) is finished_worker:
            self._delivery_queues.pop(key, None)
            self._queue_workers.pop(key, None)

    async def _queue_worker(
        self,
        key: tuple[str, str],
        queue: asyncio.Queue[v10.Task | None],
        config: TaskPushNotificationConfig,
    ) -> None:
        """Process deliveries for one config sequentially."""
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=self._idle_timeout)
            except TimeoutError:
                if not queue.empty():
                    continue
                logger.debug("Idle timeout reached for delivery queue %s", key)
                break
            if item is None:
                break
            try:
                await self._deliver_single(config, item)
            except Exception:
                logger.exception("Delivery failed for config %s", key)
            finally:
                queue.task_done()

            # Auto-cleanup after terminal state when queue is drained
            # (v1.0 state values are uppercase: TASK_STATE_*)
            if queue.empty() and item.status:
                state = (
                    item.status.state.value
                    if hasattr(item.status.state, "value")
                    else str(item.status.state)
                )
                if state in (
                    "TASK_STATE_COMPLETED",
                    "TASK_STATE_FAILED",
                    "TASK_STATE_CANCELED",
                    "TASK_STATE_REJECTED",
                ):
                    break

    async def _deliver_single(
        self,
        config: TaskPushNotificationConfig,
        task: v10.Task,
    ) -> None:
        """Deliver to a single webhook with retries.

        The connection is pinned to an IP that passed SSRF validation:
        the request URL carries the validated IP while the ``Host``
        header (and, for HTTPS, the ``sni_hostname`` extension — used by
        httpcore for both SNI and certificate hostname verification)
        carries the original hostname. This closes the TOCTOU window
        where DNS re-resolution at connect time could return a different
        (internal) address than validation saw.
        """
        url = config.url

        resolved = await resolve_webhook_url(
            url,
            allow_http=self._allow_http,
            allowed_hosts=self._allowed_hosts,
            blocked_hosts=self._blocked_hosts,
        )
        if resolved is None:
            logger.warning("Rejected webhook URL: %s", url)
            return

        assert self._http_client is not None
        headers = _build_headers(config)
        request_url = url
        extensions: dict[str, Any] = {}
        if resolved.pinned_ips:
            request_url, host_header, sni_hostname = _pin_url(url, resolved.pinned_ips[0])
            headers["Host"] = host_header
            if sni_hostname is not None:
                extensions["sni_hostname"] = sni_hostname
        # Webhooks are external clients — strip framework-internal metadata
        # keys (``_idempotency_key``, ``_a2akit_direct_reply`` etc.) exactly
        # like REST/SSE responses do. Otherwise the webhook payload leaks
        # internal state that REST clients never see.
        sanitized_task = _sanitize_task_for_client(task)
        payload = sanitized_task.model_dump(mode="json", by_alias=True, exclude_none=True)

        for attempt in range(1, self._max_retries + 1):
            async with self._semaphore:
                try:
                    resp = await self._http_client.post(
                        request_url,
                        json=payload,
                        headers=headers,
                        extensions=extensions,
                    )
                    if 200 <= resp.status_code < 300:
                        return
                    if resp.status_code < 500:
                        logger.warning("Push rejected by %s: HTTP %d", url, resp.status_code)
                        return
                    logger.warning(
                        "Push to %s failed: HTTP %d (attempt %d/%d)",
                        url,
                        resp.status_code,
                        attempt,
                        self._max_retries,
                    )
                except httpx.RequestError as exc:
                    logger.warning(
                        "Push to %s failed: %s (attempt %d/%d)",
                        url,
                        exc,
                        attempt,
                        self._max_retries,
                    )

            if attempt < self._max_retries:
                delay = self._retry_base_delay * (2 ** (attempt - 1))
                await asyncio.sleep(delay)

        logger.error(
            "Push to %s exhausted all %d retries",
            url,
            self._max_retries,
        )


def _pin_url(url: str, ip: str) -> tuple[str, str, str | None]:
    """Rewrite *url* to connect to *ip* while preserving the original host.

    Returns ``(pinned_url, host_header, sni_hostname)``. The Host header
    keeps the original hostname (plus explicit port, if any); for HTTPS
    the returned ``sni_hostname`` must be passed as an httpx request
    extension so TLS SNI and certificate verification still use the
    original hostname while the TCP connection goes to the validated IP.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    ip_ref = f"[{ip}]" if ":" in ip else ip
    if parsed.port is None:
        netloc = f"{userinfo}{ip_ref}"
        host_header = host
    else:
        netloc = f"{userinfo}{ip_ref}:{parsed.port}"
        host_header = f"{host}:{parsed.port}"
    pinned_url = urlunparse(parsed._replace(netloc=netloc))
    sni_hostname = host if parsed.scheme == "https" else None
    return pinned_url, host_header, sni_hostname


def _build_headers(config: TaskPushNotificationConfig | PushNotificationConfig) -> dict[str, str]:
    """Build HTTP headers for webhook delivery."""
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": "a2akit-push/0.1",
    }
    if config.token:
        headers["X-A2A-Notification-Token"] = config.token
    if config.authentication:
        auth = config.authentication
        if auth.credentials and auth.schemes:
            headers["Authorization"] = f"{auth.schemes[0]} {auth.credentials}"
    return headers
