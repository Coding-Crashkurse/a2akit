"""Redis-backed broker and cancel registry for multi-process deployments."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Self

from a2akit.broker.base import (
    Broker,
    CancelRegistry,
    CancelScope,
    OperationHandle,
    TaskOperation,
    _RunTask,
)
from a2akit.config import Settings, get_settings

try:
    import redis.asyncio as aioredis
except ImportError as _import_error:
    raise ImportError(
        "Redis backend requires additional dependencies. "
        "Install them with: pip install a2akit[redis]"
    ) from _import_error

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from types import TracebackType

    from a2a.types import MessageSendParams

logger = logging.getLogger(__name__)


def _serialize_operation(
    params: MessageSendParams,
    *,
    is_new_task: bool,
    request_context: dict[str, Any],
) -> str:
    """Serialize a task operation to JSON for the Redis stream."""
    # Filter internal/non-serializable keys (e.g. _otel_span, _otel_token)
    # from request_context — they are meant for in-process middleware
    # lifecycles and cannot be serialized to a Redis stream.
    safe_context = {k: v for k, v in request_context.items() if not k.startswith("_")}
    return json.dumps(
        {
            "operation": "run",
            "params": params.model_dump(mode="json", by_alias=True, exclude_none=True),
            "is_new_task": is_new_task,
            "request_context": safe_context,
        }
    )


def _deserialize_operation(fields: dict[bytes, bytes]) -> _RunTask:
    """Deserialize a Redis stream entry into a TaskOperation."""
    from a2a.types import MessageSendParams

    raw = json.loads(fields[b"op"])
    return _RunTask(
        operation="run",
        params=MessageSendParams.model_validate(raw["params"]),
        is_new_task=raw.get("is_new_task", False),
        request_context=raw.get("request_context", {}),
    )


class RedisCancelScope(CancelScope):
    """CancelScope backed by Redis Pub/Sub + local asyncio.Event.

    On construction, subscribes to the cancel channel.
    A background task listens for the cancel message and sets the local event.
    Also checks the Redis key on start (cancel may have been
    requested before the scope was created).
    """

    def __init__(
        self,
        redis_client: aioredis.Redis,
        task_id: str,
        key_prefix: str,
    ) -> None:
        self._redis = redis_client
        self._task_id = task_id
        self._key_prefix = key_prefix
        self._event = asyncio.Event()
        self._pubsub: aioredis.client.PubSub | None = None
        self._listener_task: asyncio.Task[None] | None = None
        self._startup_task: asyncio.Task[None] | None = None
        self._started = False

    async def _start(self) -> None:
        """Subscribe and check existing key."""
        if self._started:
            return
        self._started = True

        try:
            # Check if already cancelled
            cancel_key = f"{self._key_prefix}cancel:{self._task_id}"
            exists = await self._redis.exists(cancel_key)
            if exists:
                self._event.set()
                return

            # Subscribe to cancel channel
            channel = f"{self._key_prefix}cancel-ch:{self._task_id}"
            self._pubsub = self._redis.pubsub()
            await self._pubsub.subscribe(channel)

            # Re-check after subscribing (race window)
            exists = await self._redis.exists(cancel_key)
            if exists:
                self._event.set()
                await self._cleanup_pubsub()
                return
        except Exception:
            # Redis connection issue — degrade gracefully.
            # Force-cancel timeout in TaskManager is the safety net.
            logger.warning(
                "Failed to subscribe to cancel channel for %s, relying on force-cancel fallback",
                self._task_id,
            )
            await self._cleanup_pubsub()
            return

        self._listener_task = asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        """Background task listening for cancel messages."""
        try:
            if self._pubsub is None:
                return
            async for message in self._pubsub.listen():
                if message["type"] == "message":
                    self._event.set()
                    break
        except (asyncio.CancelledError, aioredis.ConnectionError):
            pass
        except Exception:
            # Unexpected error (e.g. AuthenticationError) — set the event
            # so that wait() unblocks instead of hanging forever.  The
            # force-cancel timeout in TaskManager is the ultimate safety
            # net, but there's no reason to block a semaphore slot until
            # then.
            logger.exception("Cancel listener for %s failed unexpectedly", self._task_id)
            self._event.set()
        finally:
            await self._cleanup_pubsub()

    async def _cleanup_pubsub(self) -> None:
        """Clean up pubsub resources."""
        if self._pubsub is not None:
            try:
                await self._pubsub.unsubscribe()
                await self._pubsub.aclose()
            except Exception:
                pass
            self._pubsub = None

    async def wait(self) -> None:
        """Block until cancellation is requested."""
        if self._startup_task is not None:
            await self._startup_task  # propagate startup errors instead of hanging
        elif not self._started:
            await self._start()
        await self._event.wait()

    def is_set(self) -> bool:
        """Check if cancellation was requested without blocking."""
        return self._event.is_set()


class RedisCancelRegistry(CancelRegistry):
    """Redis-backed cancel registry using SET keys + Pub/Sub notifications."""

    def __init__(
        self,
        url: str | None = None,
        *,
        pool: aioredis.ConnectionPool | None = None,
        key_prefix: str | None = None,
        ttl_s: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        s = settings or get_settings()
        self._key_prefix = key_prefix or s.redis_key_prefix
        self._ttl_s = ttl_s if ttl_s is not None else s.redis_cancel_ttl_s
        self._owns_connection = pool is None
        if pool is not None:
            self._redis = aioredis.Redis(connection_pool=pool)
        else:
            self._redis = aioredis.from_url(url or s.redis_url)
        self._scopes: list[RedisCancelScope] = []

    async def request_cancel(self, task_id: str) -> None:
        """SET cancel key + PUBLISH to cancel channel."""
        cancel_key = f"{self._key_prefix}cancel:{task_id}"
        channel = f"{self._key_prefix}cancel-ch:{task_id}"
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.set(cancel_key, "1", ex=self._ttl_s)
            pipe.publish(channel, "1")
            await pipe.execute()

    async def is_cancelled(self, task_id: str) -> bool:
        """EXISTS on cancel key."""
        cancel_key = f"{self._key_prefix}cancel:{task_id}"
        return bool(await self._redis.exists(cancel_key))

    def on_cancel(self, task_id: str) -> CancelScope:
        """Return a RedisCancelScope backed by Pub/Sub subscription."""
        scope = RedisCancelScope(self._redis, task_id, self._key_prefix)
        self._scopes.append(scope)
        # Eagerly start the scope in the background
        scope._startup_task = asyncio.create_task(scope._start())
        return scope

    async def cleanup(self, task_id: str) -> None:
        """DEL cancel key and clean up matching scopes."""
        cancel_key = f"{self._key_prefix}cancel:{task_id}"
        await self._redis.delete(cancel_key)
        remaining: list[RedisCancelScope] = []
        for scope in self._scopes:
            if scope._task_id == task_id:
                if scope._startup_task and not scope._startup_task.done():
                    scope._startup_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await scope._startup_task
                if scope._listener_task and not scope._listener_task.done():
                    scope._listener_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await scope._listener_task
                await scope._cleanup_pubsub()
            else:
                remaining.append(scope)
        self._scopes = remaining

    async def close(self) -> None:
        """Close the Redis connection if we own it."""
        # Clean up any active scopes
        for scope in self._scopes:
            if scope._startup_task and not scope._startup_task.done():
                scope._startup_task.cancel()
                with suppress(asyncio.CancelledError):
                    await scope._startup_task
            if scope._listener_task and not scope._listener_task.done():
                scope._listener_task.cancel()
                with suppress(asyncio.CancelledError):
                    await scope._listener_task
            await scope._cleanup_pubsub()
        self._scopes.clear()

        if self._owns_connection:
            await self._redis.aclose()


class RedisOperationHandle(OperationHandle):
    """Handle for a message consumed from a Redis Stream."""

    def __init__(
        self,
        *,
        redis_client: aioredis.Redis,
        stream_key: str,
        dlq_key: str,
        group_name: str,
        msg_id: bytes,
        op: TaskOperation,
        serialized_op: bytes,
        attempt: int,
        max_retries: int,
        crash_count_key: str = "",
    ) -> None:
        self._redis = redis_client
        self._stream_key = stream_key
        self._dlq_key = dlq_key
        self._group_name = group_name
        self._msg_id = msg_id
        self._op = op
        self._serialized_op = serialized_op
        self._attempt = attempt
        self._max_retries = max_retries
        self._crash_count_key = crash_count_key

    @property
    def operation(self) -> TaskOperation:
        """Return the wrapped operation."""
        return self._op

    @property
    def attempt(self) -> int:
        """Delivery attempt number (1-based)."""
        return self._attempt

    async def ack(self) -> None:
        """XACK the message."""
        await self._redis.xack(self._stream_key, self._group_name, self._msg_id)
        await self._clear_crash_count()

    async def _clear_crash_count(self) -> None:
        """Remove crash-claim tracking for this message."""
        if self._crash_count_key:
            mid = self._msg_id.decode() if isinstance(self._msg_id, bytes) else str(self._msg_id)
            await self._redis.hdel(self._crash_count_key, mid)

    async def nack(self, *, delay_seconds: float = 0) -> None:
        """XACK original + XADD new entry with attempt+1.

        If max retries are exhausted, move to DLQ instead.
        """
        next_attempt = self._attempt + 1
        if next_attempt > self._max_retries:
            # Move to dead-letter queue
            logger.error(
                "Max retries (%d) exhausted for message %s, moving to DLQ",
                self._max_retries,
                self._msg_id,
            )
            await self._redis.xadd(
                self._dlq_key,
                {b"op": self._serialized_op, b"attempt": str(next_attempt).encode()},
            )
            await self._redis.xack(self._stream_key, self._group_name, self._msg_id)
            await self._clear_crash_count()
            return

        # NOTE: delay_seconds is accepted for API compatibility but NOT
        # enforced via sleep here — sleeping would hold the caller's
        # semaphore slot, starving the worker.  The re-added message is
        # immediately available; Redis XAUTOCLAIM's min_idle_time provides
        # natural backoff for repeatedly failing messages.

        # ACK original, re-add with incremented attempt
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.xack(self._stream_key, self._group_name, self._msg_id)
            pipe.xadd(
                self._stream_key,
                {b"op": self._serialized_op, b"attempt": str(next_attempt).encode()},
            )
            await pipe.execute()
        await self._clear_crash_count()


class RedisBroker(Broker):
    """Redis Streams-backed broker for multi-process deployments.

    Uses a single consumer group so each message is delivered to
    exactly one consumer. Stale messages from dead consumers are
    periodically reclaimed via XAUTOCLAIM.
    """

    def __init__(
        self,
        url: str | None = None,
        *,
        pool: aioredis.ConnectionPool | None = None,
        stream_name: str | None = None,
        group_name: str | None = None,
        consumer_prefix: str | None = None,
        block_ms: int | None = None,
        claim_timeout_ms: int | None = None,
        key_prefix: str | None = None,
        max_retries: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        s = settings or get_settings()
        self._key_prefix = key_prefix or s.redis_key_prefix
        stream = stream_name or s.redis_broker_stream
        self._stream_key = f"{self._key_prefix}stream:{stream}"
        self._dlq_key = f"{self._key_prefix}dlq:{stream}"
        self._crash_count_key = f"{self._key_prefix}crash_counts"
        self._group_name = group_name or f"{self._key_prefix}{s.redis_broker_group}"
        prefix = consumer_prefix or s.redis_broker_consumer_prefix
        self._consumer_name = (
            f"{prefix}-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        self._block_ms = block_ms if block_ms is not None else s.redis_broker_block_ms
        self._claim_timeout_ms = (
            claim_timeout_ms if claim_timeout_ms is not None else s.redis_broker_claim_timeout_ms
        )
        self._max_retries = max_retries if max_retries is not None else s.max_retries
        self._shutdown_flag = False
        self._owns_connection = pool is None
        self._url = url or s.redis_url
        self._pool = pool
        self._redis: aioredis.Redis | None = None
        self._claim_task: asyncio.Task[None] | None = None

    @property
    def _r(self) -> aioredis.Redis:
        if self._redis is None:
            raise RuntimeError("RedisBroker not connected — use 'async with broker' first")
        return self._redis

    async def __aenter__(self) -> Self:
        """Create Redis client, ensure consumer group exists, start claim task."""
        if self._pool is not None:
            self._redis = aioredis.Redis(connection_pool=self._pool)
        else:
            self._redis = aioredis.from_url(self._url)

        try:
            # Verify connectivity
            await self._redis.ping()

            # Create consumer group (MKSTREAM creates the stream if needed)
            try:
                await self._redis.xgroup_create(
                    self._stream_key, self._group_name, id="0", mkstream=True
                )
                logger.info(
                    "Created consumer group %s on stream %s",
                    self._group_name,
                    self._stream_key,
                )
            except aioredis.ResponseError as e:
                if "BUSYGROUP" not in str(e):
                    raise
                # Group already exists — fine
        except Exception:
            if self._owns_connection:
                await self._redis.aclose()
            raise

        logger.info("Redis broker consumer registered: %s", self._consumer_name)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Cancel claim task, delete consumer, close connection."""
        if self._claim_task and not self._claim_task.done():
            self._claim_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._claim_task

        if self._redis:
            try:
                await self._redis.xgroup_delconsumer(
                    self._stream_key, self._group_name, self._consumer_name
                )
                logger.info("Redis broker consumer deregistered: %s", self._consumer_name)
            except Exception:
                logger.warning("Failed to deregister consumer %s", self._consumer_name)

            if self._owns_connection:
                await self._redis.aclose()

    async def health_check(self) -> dict[str, Any]:
        """Ping Redis to verify connectivity."""
        try:
            if self._redis:
                await self._redis.ping()
            return {"status": "ok"}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    async def run_task(
        self,
        params: MessageSendParams,
        *,
        is_new_task: bool = False,
        request_context: dict[str, Any] | None = None,
    ) -> None:
        """XADD a task operation to the stream."""
        serialized = _serialize_operation(
            params,
            is_new_task=is_new_task,
            request_context=request_context or {},
        )
        await self._r.xadd(
            self._stream_key,
            {b"op": serialized.encode(), b"attempt": b"1"},
        )

    async def shutdown(self) -> None:
        """Signal receive_task_operations to stop."""
        self._shutdown_flag = True

    async def receive_task_operations(self) -> AsyncIterator[OperationHandle]:
        """Yield task operations via XREADGROUP (blocking)."""

        while not self._shutdown_flag:
            # 1. Claim stale messages from dead consumers
            claimed_any = False
            try:
                async for handle in self._claim_stale_messages():
                    yield handle
                    claimed_any = True
            except aioredis.ConnectionError:
                logger.warning("Redis connection lost during claim, retrying...")
                await asyncio.sleep(1)
                continue

            # 2. Read new messages (blocking)
            # After claims, use non-blocking read (block=None) to loop back
            # immediately for more stale messages. block=0 means "block forever"
            # in Redis, so we must use None for non-blocking.
            try:
                entries = await self._r.xreadgroup(
                    groupname=self._group_name,
                    consumername=self._consumer_name,
                    streams={self._stream_key: ">"},
                    count=1,
                    block=None if claimed_any else self._block_ms,
                )
            except aioredis.ConnectionError:
                logger.warning("Redis connection lost, retrying...")
                await asyncio.sleep(1)
                continue

            if not entries:
                continue  # Block timeout, loop back

            for _stream_name, messages in entries:
                for msg_id, fields in messages:
                    try:
                        op = _deserialize_operation(fields)
                        attempt = int(fields.get(b"attempt", b"1"))
                        yield RedisOperationHandle(
                            redis_client=self._r,
                            stream_key=self._stream_key,
                            dlq_key=self._dlq_key,
                            group_name=self._group_name,
                            msg_id=msg_id,
                            op=op,
                            serialized_op=fields[b"op"],
                            attempt=attempt,
                            max_retries=self._max_retries,
                            crash_count_key=self._crash_count_key,
                        )
                    except Exception:
                        logger.exception("Failed to deserialize operation %s", msg_id)
                        await self._r.xadd(
                            self._dlq_key,
                            {
                                b"op": fields.get(b"op", b""),
                                b"error": b"deserialization_failed",
                            },
                        )
                        await self._r.xack(self._stream_key, self._group_name, msg_id)

    async def _claim_stale_messages(self) -> AsyncIterator[OperationHandle]:
        """Reclaim messages from dead consumers via XAUTOCLAIM."""
        try:
            result = await self._r.xautoclaim(
                self._stream_key,
                self._group_name,
                self._consumer_name,
                min_idle_time=self._claim_timeout_ms,
                count=1,
            )
            # result is (next_start_id, [(msg_id, fields), ...], [deleted_ids])
            claimed_messages = result[1] if len(result) > 1 else []
            for msg_id, fields in claimed_messages:
                if not fields:
                    # Deleted entry, just ACK it
                    await self._r.xack(self._stream_key, self._group_name, msg_id)
                    continue
                try:
                    op = _deserialize_operation(fields)
                    base_attempt = int(fields.get(b"attempt", b"1"))
                    # Track hard-crash claims to detect poison pill messages.
                    # Workers that die (OOM/segfault) never call nack(), so
                    # the payload's attempt counter stays frozen. This hash
                    # counts XAUTOCLAIM re-deliveries to catch the loop.
                    mid = msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id)
                    claim_count = await self._r.hincrby(self._crash_count_key, mid, 1)
                    effective_attempt = base_attempt + claim_count
                    if effective_attempt > self._max_retries:
                        logger.error(
                            "Poison pill: message %s claimed %d times, moving to DLQ",
                            msg_id,
                            claim_count,
                        )
                        await self._r.xadd(
                            self._dlq_key,
                            {
                                b"op": fields[b"op"],
                                b"attempt": str(effective_attempt).encode(),
                            },
                        )
                        await self._r.xack(self._stream_key, self._group_name, msg_id)
                        await self._r.hdel(self._crash_count_key, mid)
                        # Yield a handle so the WorkerAdapter can mark
                        # the task as failed in storage (the broker has
                        # no access to storage/emitter).
                        yield RedisOperationHandle(
                            redis_client=self._r,
                            stream_key=self._stream_key,
                            dlq_key=self._dlq_key,
                            group_name=self._group_name,
                            msg_id=msg_id,
                            op=op,
                            serialized_op=fields[b"op"],
                            attempt=effective_attempt,
                            max_retries=self._max_retries,
                            crash_count_key=self._crash_count_key,
                        )
                        continue
                    logger.warning(
                        "Claimed stale message %s (attempt %d)", msg_id, effective_attempt
                    )
                    yield RedisOperationHandle(
                        redis_client=self._r,
                        stream_key=self._stream_key,
                        dlq_key=self._dlq_key,
                        group_name=self._group_name,
                        msg_id=msg_id,
                        op=op,
                        serialized_op=fields[b"op"],
                        attempt=effective_attempt,
                        max_retries=self._max_retries,
                        crash_count_key=self._crash_count_key,
                    )
                except Exception:
                    logger.exception("Failed to deserialize claimed message %s", msg_id)
                    await self._r.xadd(
                        self._dlq_key,
                        {
                            b"op": fields.get(b"op", b""),
                            b"error": b"deserialization_failed",
                        },
                    )
                    await self._r.xack(self._stream_key, self._group_name, msg_id)
        except aioredis.ResponseError:
            # Stream or group doesn't exist yet — nothing to claim
            pass


def redis_task_lock_factory(
    redis_client: aioredis.Redis,
    timeout: int = 300,
) -> Callable[[str], Any]:
    """Return a task lock factory using Redis distributed locks.

    Usage::

        adapter = WorkerAdapter(
            worker=MyWorker(),
            broker=redis_broker,
            ...,
            task_lock_factory=redis_task_lock_factory(redis_client, timeout=300),
        )
    """

    def _create_lock(task_id: str) -> Any:
        return redis_client.lock(f"a2akit:tasklock:{task_id}", timeout=timeout)

    return _create_lock
