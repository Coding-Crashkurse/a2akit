"""Webhook delivery service - sends task updates to client-provided URLs."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

from a2akit.push.validation import validate_webhook_url

if TYPE_CHECKING:
    from a2a.types import Task

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
    ) -> None:
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrent_deliveries)
        self._allow_http = allow_http
        self._allowed_hosts = allowed_hosts
        self._blocked_hosts = blocked_hosts
        self._http_client: httpx.AsyncClient | None = None
        # Per-config delivery queues ensure sequential ordering
        # Key: (task_id, config_id)
        self._delivery_queues: dict[tuple[str, str], asyncio.Queue[Task | None]] = {}
        self._queue_workers: dict[tuple[str, str], asyncio.Task[None]] = {}

    async def startup(self) -> None:
        """Initialize the HTTP client."""
        self._http_client = httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,  # Security: no redirect following
            verify=True,  # TLS verification mandatory
        )

    async def shutdown(self) -> None:
        """Gracefully shut down all delivery workers."""
        for queue in self._delivery_queues.values():
            queue.put_nowait(None)  # Sentinel
        if self._queue_workers:
            await asyncio.wait(self._queue_workers.values(), timeout=30.0)
        if self._http_client:
            await self._http_client.aclose()

    async def deliver(
        self,
        configs: list[TaskPushNotificationConfig],
        task: Task,
    ) -> None:
        """Fan out delivery to all webhook configs for a task.

        Each config gets its own sequential queue so events arrive
        in order. Different configs are delivered in parallel.
        """
        for config in configs:
            config_id = config.push_notification_config.id or "default"
            queue_key = (task.id, config_id)

            if queue_key not in self._delivery_queues:
                queue: asyncio.Queue[Task | None] = asyncio.Queue()
                self._delivery_queues[queue_key] = queue
                worker = asyncio.create_task(self._queue_worker(queue_key, queue, config))
                self._queue_workers[queue_key] = worker
                worker.add_done_callback(
                    lambda fut, k=queue_key: self._cleanup_queue(k)  # type: ignore[misc]
                )

            self._delivery_queues[queue_key].put_nowait(task)

    def _cleanup_queue(self, key: tuple[str, str]) -> None:
        self._delivery_queues.pop(key, None)
        self._queue_workers.pop(key, None)

    async def _queue_worker(
        self,
        key: tuple[str, str],
        queue: asyncio.Queue[Task | None],
        config: TaskPushNotificationConfig,
    ) -> None:
        """Process deliveries for one config sequentially."""
        while True:
            item = await queue.get()
            if item is None:
                break
            try:
                await self._deliver_single(config, item)
            except Exception:
                logger.exception("Delivery failed for config %s", key)
            finally:
                queue.task_done()

            # Auto-cleanup after terminal state when queue is drained
            if queue.empty() and item.status:
                state = (
                    item.status.state.value
                    if hasattr(item.status.state, "value")
                    else str(item.status.state)
                )
                if state in ("completed", "failed", "canceled", "rejected"):
                    break

    async def _deliver_single(
        self,
        config: TaskPushNotificationConfig,
        task: Task,
    ) -> None:
        """Deliver to a single webhook with retries."""
        pnc = config.push_notification_config
        url = pnc.url

        if not validate_webhook_url(
            url,
            allow_http=self._allow_http,
            allowed_hosts=self._allowed_hosts,
            blocked_hosts=self._blocked_hosts,
        ):
            logger.warning("Rejected webhook URL: %s", url)
            return

        assert self._http_client is not None
        headers = _build_headers(pnc)
        payload = task.model_dump(mode="json", by_alias=True, exclude_none=True)

        async with self._semaphore:
            for attempt in range(1, self._max_retries + 1):
                try:
                    resp = await self._http_client.post(
                        url,
                        json=payload,
                        headers=headers,
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


def _build_headers(config: PushNotificationConfig) -> dict[str, str]:
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
