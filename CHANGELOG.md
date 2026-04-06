# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [0.0.30] — 2026-04-06

### Fixed
- **OCC race condition in follow-up submissions** — `_submit_task` fetched the
  OCC version *after* validating task state, allowing two concurrent follow-ups
  to both pass the `input_required` check and both succeed. Version is now
  captured *before* validation so the second writer correctly gets
  `ConcurrencyError`. Same TOCTOU fix applied to `cancel_task_in_storage` and
  `_mark_failed`.
- **SSE event duplication / loss between subscribe and snapshot** — events
  published between `event_bus.subscribe()` and `storage.load_task()` could
  appear in both the snapshot and the live stream (Memory backend) or be lost
  entirely (Redis backend). Both backends now capture the stream position at
  subscribe time and use it as the dedup/replay baseline.
- **Redis nack burned retries instantly** — `XACK` + `XADD` made retry messages
  immediately available; exponential backoff was ignored. Messages now carry a
  `not_before` timestamp and the consumer sleeps until it passes. The message
  stays durably in the stream (no more `asyncio.create_task` with in-RAM sleep),
  preserving the XAUTOCLAIM recovery path.
- **Artifact loss on force-cancel race** — when a force-cancel set the terminal
  state while the worker was mid-flight, `TaskTerminalStateError` was caught but
  pending artifacts (already sent via SSE) were never flushed to storage. All
  three affected paths (`_run_task_inner`, `_mark_failed`,
  `cancel_task_in_storage`) now perform an artifact-only fallback write
  (`state=None` bypasses the terminal guard).
- **False OTel error span after successful lifecycle call** — an exception thrown
  *after* `ctx.complete()` (e.g. cleanup code) marked the span as `ERROR` and
  attempted `_mark_failed` on an already-completed task. The handler now checks
  `ctx.turn_ended` and records a warning event instead.
- **RedisCancelScope deaf after transient Redis failure** — `_started` is now
  reset on failure, and `wait()` retries `_start()` when the startup task
  completed without successfully subscribing (was dead code due to
  `elif`→`if` precedence bug).
- **Middleware / PubSub leak on early client disconnect** — `_stream_setup` and
  `_subscribe_setup` converted from plain `return` to `yield`-based FastAPI
  dependencies so cleanup runs even if the route handler is never entered.
- **Resource leak on mid-execution task deletion** — worker cleanup now treats
  `load_task() → None` as terminal, running full event-bus and cancel-registry
  cleanup instead of skipping it.
- **Dead `_claim_task` code removed** — ghost field from an earlier
  implementation; stale-message reclaim runs inline in `receive_task_operations`.

## [0.0.29] — 2026-04-05

### Fixed
- **JSON-RPC `message/stream` spec compliance** — dispatch now registers the
  spec-compliant method name; `message/sendStream` kept as compat alias.
- **Middleware `after_dispatch` leak on auth rejection** — all dispatch sites
  now track which middlewares completed `before_dispatch` and only roll those
  back, preventing OTel span/token leaks on unauthenticated requests.
- **`tasks/resubscribe` tracing span ended before SSE stream** — moved to
  self-handled middleware list so `after_dispatch` defers to the SSE finally.
- **`_versioned_update` overwrote cancel message** — terminal-state guard now
  also fires for status-message-only writes on terminal tasks.
- **`_force_cancel_after` cleanup chain** — each cleanup step wrapped in its
  own `try/except` so a Redis blip in one doesn't skip the other.
- **`RedisEventBus.subscribe` PubSub connection leak** on subscribe failure.
- **`RedisEventBus` crash on invalid `Last-Event-ID`** — falls back to
  replay-from-start instead of crashing in gap-fill.
- **JSON-RPC client `subscribe_task`** now sends `Last-Event-ID` header
  instead of polluting params.
- **Webhook payload** no longer leaks internal metadata keys.
- **SSRF allow-list** delegates to `ip.is_global` (covers `0.0.0.0` and all
  IANA special-purpose ranges).
- **Push-config cascade** on task/context deletion.
- **Push delivery ordering** — inline dispatch preserves state-transition order.
- **Worker semaphore backpressure** — acquired before pulling from broker.
- **Artifacts at cancel time** written atomically with the cancel transition.

## [0.0.28] — 2026-04-05

### Fixed
- **Cancel signal lost on `input_required` turns** — cancel key preserved
  across non-terminal turns; only per-turn Pub/Sub resources released.
- **Double-enqueue on idempotent `create_task` retry** — storage signals
  genuine inserts via transient metadata marker.
- **WebhookDeliveryService shutdown race** — workers force-cancelled after
  grace period before HTTP client close.

## [0.0.27] — 2026-04-04

### Fixed
- **RedisCancelScope false-positive cancellation** — no longer sets the cancel
  event on Redis connection failures; force-cancel timeout is the safety net.

## [0.0.26] — 2026-04-03

### Fixed
- Client text-streaming corruption (`"\n".join` → `"".join`).
- Poison pill `ack` moved to `finally` block.
- Redis `BLOCK 0` deadlock in broker loop.
- Redis `__aenter__` connection leak on startup failure (all three backends).
- `XAUTOCLAIM` `ConnectionError` crash in broker loop.
- OTel + Redis serialization crash (filter `_`-prefixed keys).
- Push webhook state ordering (inline snapshot load).
- CancelRegistry Pub/Sub cleanup on every turn.
- Redis idempotency key cleanup on task/context deletion.
- Transport fallback for 5xx responses.
- `ConcurrencyError` terminal detection in `_submit_task`.
- Worker cleanup restricted to terminal tasks (preserves replay buffers).
- Various: BaseException artifact recovery, nack exception handling, shutdown
  vs cancel distinction, lifecycle double-call guard, send_status turn guard,
  SQL COUNT performance, defensive Redis deserialization, pageToken handling,
  stable sort tie-breaker, client close resilience.

## [0.0.25] — 2026-04-03

### Fixed
- Shutdown vs user-cancel distinction (re-raise for broker retry).
- XAUTOCLAIM recovery speed (`block=None` after claims).
- Poison pill pre-dispatch check and mark_failed-before-ack ordering.
- `ConcurrencyError` resilience in `_submit_task` (re-check idempotency).
- `send_status` / `_flush_artifacts` ConcurrencyError recovery.
- `_terminal_transition` cancel shield for post-write SSE emission.
- `subscribe_task` subscribe-before-load ordering and terminal reconnect.
- `stream_message` duplicate/terminal detection.
- SSE reconnect deduplication and RedisEventBus gap-fill for new subscribers.
- Redis EventBus safety poll throttle (~97% reduction in idle Redis load).
- Redis cleanup EXPIRE instead of DELETE (60s grace for active subscribers).
- SSE generator/middleware cleanup resilience.
- Follow-up idempotency, context_id handling, enqueue prevention.
- Cancel spam guard, RedisCancelScope graceful degradation.
- OCC retry with fresh version, Redis event stream fallback TTL.
- SSRF bypass via IPv6-mapped IPv4, Content-Type validation for chunked transfers.
- Various client fixes (SSE read timeout, REST Content-Type, transport fallback).

## [0.0.24] — 2026-04-01

### Fixed
- SSRF bypass via IPv6-mapped IPv4.
- `_enqueue_or_fail` publishes final SSE event on broker failure.
- Follow-up idempotency prevents re-enqueue.
- Cleanup chain resilience (individually wrapped try/except).
- `RedisCancelRegistry` connection leak on shutdown.
- Redis idempotency key TTL (24h).
- Corrupt broker payloads moved to DLQ.
- `ConcurrencyError` mapped to HTTP 409 / JSON-RPC -32004.
- Enum repr in error messages.
- Client SSE read timeout.
- Redis EventBus safety poll throttle.
- Invalid `pageToken` handling.
- Client `close()` resilience.

## [0.0.23] — 2026-03-31

### Fixed
- Blocking timeout returns task state instead of error (spec §3.1.2).
- Broker failure in blocking/streaming paths marks task as failed.
- Follow-up message idempotency via `messageId` dedup.
- Middleware OTel span leak on `send_message` error.
- Redis EventBus SSE hang (1s fallback poll).
- Redis task lock factory (async→sync).
- Client SSE multi-line parsing (W3C spec compliance).
- JSON-RPC streaming error detection via Content-Type check.
- Webhook delivery race conditions (idle-timeout, worker identity).
- Redis broker poison pill tracking via crash counts + DLQ.
- AnyIO cancellation cleanup with `CancelScope(shield=True)`.
- SQL pagination deterministic sort.
- Stale status timestamp on message-only updates.
- FastAPI 422 → A2A error format.
- Redis storage defensive deserialization.
- Telemetry version from package metadata.
- JSON-RPC `params: null` crash and auth error code.
- `A2AClient` HTTP client leak on connect failure.

## [0.0.22] — 2026-03-29

### Fixed
- SSE Event-ID mismatch (use bus-assigned IDs for `Last-Event-ID` replay).
- JSON-RPC streaming error handling (eager first-event evaluation).
- Dependency shutdown order (user deps after worker adapter).
- Push delivery semaphore scope and queue race condition.
- Agent message metadata leak.
- Readiness endpoint resilience.
- AgentCard `additionalInterfaces` spec compliance.
- InMemoryEventBus replay deduplication.
- SQL storage stale OCC version.
- Redis cancel scope hang on unexpected exceptions.
- `params.message` mutation, cancel `context_id` fallback.
- PushDeliveryEmitter shutdown.
- `respond("")` empty parts, `request_auth(details="")` ignored.
- Redis Stream ID comparison, artifact metadata default.

## [0.0.21] — 2026-03-28

### Added
- **Redis storage backend** — Redis Hashes + Lua scripts for atomic OCC.
  Auto-detected with `redis://` URL. Install with `pip install a2akit[redis]`.
- Push delivery queue idle timeout (default 300s, configurable).

### Fixed
- Redis CancelScope leak on cleanup.

## [0.0.20] — 2026-03-28

### Added
- Deploying to Production guide (Docker Compose + Redis + PostgreSQL).
- Troubleshooting / FAQ page.
- TaskContext Quick Reference table.

### Changed
- Consistent REST error format (spec §3.2.3).
- Blocking timeout raises `UnsupportedOperationError` (spec §7.1.2).
- JSON-RPC `tasks/resubscribe` passes `lastEventId` (spec §3.4.1).
- Delete push config returns 200 with null (spec §7.8).
- HTTP webhook warning on insecure URLs.

### Fixed
- SQL `_trim_history` empty list handling.
- `ConcurrencyError` in InMemoryStorage includes `current_version`.
- Repository rename references.
- Echo example `request_input()` demo.

## [0.0.19] — 2026-03-28

### Changed
- **Deferred storage for streaming tasks** — intermediate DB writes skipped
  for SSE clients; only the terminal write persists (1 write instead of ~7–9).
- **Redis EventBus single-roundtrip publish** — pipelined XADD + PUBLISH.
- Eliminated JSON double-serialization (18 occurrences).
- `ConcurrencyError` carries `current_version`.
- Debug UI served from static files.
- Robust server shutdown on partial startup failures.

### Fixed
- Debug UI `preferredTransport` case-insensitive comparison.

## [0.0.18] — 2026-03-25

### Added
- **Redis Broker** (`RedisBroker`) — Redis Streams-backed task queue with consumer
  groups, automatic stale-message recovery via `XAUTOCLAIM`, dead-letter queue,
  and configurable retry semantics. Drop-in replacement for `InMemoryBroker`.
- **Redis EventBus** (`RedisEventBus`) — Pub/Sub for live fan-out + Streams for
  replay buffer. Supports `Last-Event-ID` based reconnection with gap-fill pattern.
  Drop-in replacement for `InMemoryEventBus`.
- **Redis CancelRegistry** (`RedisCancelRegistry`) — SET keys for durability +
  Pub/Sub channels for real-time notification. `RedisCancelScope` blocks on
  Pub/Sub instead of polling. Drop-in replacement for `InMemoryCancelRegistry`.
- **Connection string activation** — pass `broker="redis://..."` and
  `event_bus="redis://..."` to `A2AServer`. Cancel registry auto-creates from
  broker URL when not explicitly provided.
- **Shared connection pool** — when broker and event bus use the same Redis URL,
  pass an explicit `ConnectionPool` to avoid 3x connections.
- **`redis_task_lock_factory`** — convenience distributed lock for task-level
  serialization across multiple consumers.
- **`pip install a2akit[redis]`** — new optional dependency group.
