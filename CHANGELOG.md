# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [0.0.36] — hardening: security, v1.0 wire correctness, backend durability

Post-migration review pass fixing verified bugs across the whole stack.
Several items are behavior changes — read the Security and Changed
sections before upgrading.

### Security
- **Agent-card signature verification actually runs for v1.0 cards.**
  `A2AClient.connect()` previously verified a v0.3 projection that had
  already dropped the `signatures` field, so "soft" mode never verified
  (a MITM could alter the card undetected) and "strict" mode rejected
  every validly signed v1.0 card. Verification now uses the v1.0 card.
- **`jku` JWKS fetching is off by default.** The verification key is no
  longer fetched from the attacker-controlled JWS header URL unless an
  explicit `allowed_jku_hosts` allowlist is provided. Without an
  allowlist the key must come from `trusted_keys`. Closes a
  verification-bypass and an SSRF vector.
- **`X-Forwarded-Proto`/`X-Forwarded-Host` are no longer trusted by
  default** when building agent-card URLs. Set the new
  `trust_proxy_headers` setting (`A2AKIT_TRUST_PROXY_HEADERS=true`) when
  running behind a trusted reverse proxy.
- **Webhook delivery is pinned to the DNS answer seen at SSRF
  validation** (Host/SNI preserve the original hostname), closing a
  DNS-rebinding TOCTOU where validation saw a public IP and delivery
  re-resolved to an internal address.
- **API-key comparison is constant-time** (`hmac.compare_digest`) and
  the `Bearer` scheme is matched case-insensitively per RFC 7235.
- Fallback internal-error responses no longer echo arbitrary exception
  text to the client; the detail is logged server-side.

### Fixed
- **v1.0 push-config create was always rejected (HTTP 400)** on both
  REST and JSON-RPC — the handlers wrapped the config in a shape
  `handle_set_config` did not accept. Now fixed.
- v1.0 JSON-RPC push Get/List/Delete return `-32003`
  (PUSH_NOTIFICATIONS_NOT_SUPPORTED) instead of crashing to `-32603`
  when push is disabled.
- v1.0 REST push-config DELETE returns a clean `204` (no body) instead
  of triggering an ASGI `RuntimeError` under uvicorn.
- v1.0 `/health/ready` returns HTTP `503` when a backend is degraded
  (was always `200`, so readiness probes never failed).
- v0.3 JSON-RPC SSE terminal events now carry `final: true`.
- v1.0 SSE parsers no longer crash on a bare `Message` event and no
  longer drop the final event when the stream closes without a trailing
  blank line.
- JSON-RPC dispatchers reject non-object `params` with `-32602` instead
  of returning a plain-text HTTP 500.
- v1.0 `historyLength: 0` is honored (was dropped as falsy, returning
  full history).
- `list_tasks(status_timestamp_after=...)` no longer raises `TypeError`
  on the in-memory backend.
- SQL `list_tasks` tenant filter is applied before pagination, fixing
  overstated `total_size` and empty-but-non-terminal pages.
- The worker's broker-consumption loop survives a broker iterator crash
  (malformed stream entry, transient Redis error) with backoff instead
  of tearing down the server.
- `message/stream` persists `tenant` (was send-only) and honors
  `history_length` on retried requests.
- A broker-enqueue error can no longer mark a task failed after a worker
  has already picked it up.
- In-memory `update_task` is now atomic on partial failure and deep-copies
  inputs (no aliasing of caller objects); the in-memory event bus keeps
  replay buffers for a 60s grace window after terminal cleanup.
- Redis `delete_task` is atomic and returns `False` when a concurrent
  deleter wins; `redis_task_lock_factory` honors `redis_key_prefix`;
  RedisStorage tolerates `decode_responses=True` pools.
- SQLite sets `PRAGMA busy_timeout=5000` so concurrent writers no longer
  get instant "database is locked" errors.
- Invalid `A2AKIT_LOG_LEVEL` falls back to `INFO` with a warning instead
  of raising from an arbitrary call site.
- A `Dependency` registered under multiple keys starts and stops exactly
  once.

### Changed
- **Removed the internal `deferred_storage` optimization.** Intermediate
  `send_status` writes and periodic artifact flushes now always persist,
  including for `return_immediately=True` clients (which poll
  `tasks/get`, so suppressing persistence was inverted). `send_status`
  events are also broadcast *after* the storage write to preserve SSE
  ordering.
- `redis_broker_claim_timeout_ms` default raised from `60000` to
  `600000` (10 min). The old default let another consumer re-claim and
  duplicate any task longer than 60s (routine for LLM workloads). For
  multi-worker deployments prefer `redis_task_lock_factory`.
- SQL blind writes (`expected_version=None`) retry on contention instead
  of raising a spurious `ConcurrencyError`, matching Redis semantics.
- SQL blind `create_task` re-read raises `TaskNotFoundError` instead of a
  bare `assert`.
- Per-config webhook delivery queues are bounded (`max_queue_size`,
  default 100) with drop-oldest; registering an SSRF-blocked or invalid
  webhook URL now fails fast (400 / JSON-RPC error) instead of being
  accepted and silently dropped at delivery.
- Task metrics (`a2akit.task.*`) are recorded again — the emitter now
  compares against v1.0 `TaskState` values instead of dead v0.3 strings.
- Server telemetry spans are marked `ERROR` (with recorded exception) on
  failed requests instead of always `OK`.

### Added
- `trust_proxy_headers` (`A2AKIT_TRUST_PROXY_HEADERS`, default `false`).
- `redis_broker_stream_maxlen` (`A2AKIT_REDIS_BROKER_STREAM_MAXLEN`,
  default `10000`) — approximate MAXLEN for the broker task stream and
  DLQ, bounding previously unbounded Redis growth.
- `redis_idempotency_ttl_s` (`A2AKIT_REDIS_IDEMPOTENCY_TTL_S`, default
  `86400`) — configurable Redis idempotency-key TTL.
- v1.0 SSE streaming, v1.0 push-config CRUD, signature-verification
  end-to-end, hard-cancel / shutdown-redelivery, and backend-contention
  test coverage for the paths above.

### API notes
- `a2akit._signatures.verify_agent_card` / `verify_signature` are now
  async coroutines.
- `TaskContextImpl.__init__` and `ContextFactory.build` no longer accept
  `deferred_storage` (internal API).

## [0.0.35] — UNRELEASED (single-version servers, typed mismatch)

### Removed
- **Dual-protocol serving.** `A2AServer(protocol_version={"1.0", "0.3"})`
  now raises `ValueError` at init — each server serves exactly one wire
  version. Mixed-fleet users should run two `A2AServer` instances behind
  a reverse proxy. Deleted `_dual_jsonrpc.py`, `_register_exception_
  handlers_dual`, `include_v03_interfaces` card-builder mode,
  `normalize_protocol_versions`, and the dual-mode integration tests.

### Added
- **`ProtocolVersionMismatchError`** (in `a2akit.client.errors`) raised
  when the client can't speak a server's wire version — either because
  the agent card advertises only unsupported versions, or because the
  server rejects an `A2A-Version` header mismatch. All four client
  transports (`RestTransport`, `JsonRpcTransport`, `RestV10Transport`,
  `JsonRpcV10Transport`) map the server's 400-response envelopes to this
  typed exception.
- **Connect-time pre-flight** in `A2AClient.connect()` that rejects
  agent cards advertising only unspeakable protocol versions without
  making a round-trip.

### Changed
- `A2AServer.protocol_version` is now a single `ProtocolVersion` instead
  of a set. `app.state.protocol_version` is exposed for telemetry.
- `build_agent_card` / `build_discovery_router` take `protocol_version=`
  (single) instead of `protocol_versions=` (iterable).
- `build_agent_card_v10` drops the `include_v03_interfaces` kwarg.

## [0.0.34] — UNRELEASED (A2A v1.0 migration complete)

### Added
- **Native A2A v1.0 wire stack** — `endpoints_v10.py` (bare-path REST
  `/message:send`, `/tasks/{id}`, wrapped SSE discriminator) and
  `jsonrpc_v10.py` (PascalCase `SendMessage`/`GetTask`/…). Framework
  default is now v1.0; v0.3 lives under `/v1/` for legacy clients.
- **Dual-protocol mode** (`protocol_version={"1.0", "0.3"}`) — same server
  accepts both wire formats. REST disambiguates by path prefix; JSON-RPC
  at shared `POST /` routes by method-name shape (slash = v0.3,
  PascalCase = v1.0). `_dual_jsonrpc.py` stamps the per-request
  `A2A-Version` so responses echo the right version on the shared endpoint.
- **Agent-card signature verification** — detached JWS (RFC 7515) + JCS
  canonicalization (RFC 8785). `A2AClient(verify_signatures="soft"|"strict"|"off")`
  with optional trusted key list and JKU allow-list. Server-side gate via
  `SignatureVerificationConfig`. Requires the `signatures` extra.
- **`google.rpc.Status` error catalog** (`_errors_v10.py`) — 12 exception
  types map to HTTP status + JSON-RPC code + gRPC status + `ErrorInfo.reason`.
- **Native v1.0 client transports** — `RestV10Transport`, `JsonRpcV10Transport`
  accept v03 `MessageSendParams` from the existing client API and convert
  to/from the v1.0 wire shape transparently. `A2AClient` auto-detects
  `supportedInterfaces[]` on the agent card and picks the right transport.
- **Flat `TaskPushNotificationConfig`** — matches the v1.0 spec
  (`{taskId, id, url, token, authentication}`). The legacy nested
  `push_notification_config` accessor remains as a back-compat property
  for existing code.

### Changed
- **Default `protocol_version` is now `"1.0"`.** Set
  `A2AKIT_DEFAULT_PROTOCOL_VERSION=0.3` or pass `protocol_version="0.3"`
  to `A2AServer` for legacy behavior.
- v0.3 JSON-RPC push endpoints now serialize via `_serialize_tpnc_v03`
  (nested shape on the wire); v1.0 endpoints use `_serialize_tpnc`
  (flat shape on the wire). Internal representation is always flat.
- `A2AClient` exposes `verify_signatures`, `trusted_signing_keys`,
  `allow_jku_fetch`, `allowed_jku_hosts` kwargs.

### Documentation
- New **Protocol Versions guide** (`docs/guides/protocol-versions.md`)
  covering single-version / dual-mode / client auto-detection /
  signature verification.
- README gains a Protocol Versions section and a full v0.3-vs-v1.0 wire
  comparison.
- Examples updated: `multi_transport`, `output_negotiation`,
  `agent_card`, `extensions` now use v1.0 shapes in their docstrings and
  sample bodies.

## [0.0.33] — UNRELEASED (A2A v1.0 migration scaffolding)

### Added
- **A2A v1.0 scaffolding**: internal state is now `a2a_pydantic.v10`; v0.3
  clients keep working via a compat layer that converts bodies at the wire
  boundary.
- **`ProtocolVersion` enum** and `A2AServer(..., protocol_version=...)` kwarg
  (accepts `"1.0"`, `"0.3"`, or a set of both — spec §2).
- **Version-aware AgentCard builder** — `build_agent_card_v10` emits the v1.0
  `supported_interfaces[]` shape; `build_agent_card_v03` keeps the v0.3 shape.
- **`RequestEnvelope.tenant`** — v1.0 multi-tenancy as a first-class
  middleware attribute (§8).
- **`ListTasksQuery.tenant`** filter, honored by all storage backends (§9).
- **Uses `a2a_pydantic.convert_to_v10(...)` at the v0.3 wire boundary.**
  Requires `a2a-pydantic>=0.0.9`. Three upstream releases (0.0.6 →
  `convert_to_v10` + `validate_assignment`, 0.0.8 → Part input coercion
  + sensible defaults, 0.0.9 → `Struct` as `MutableMapping`) let us
  delete the `_v03_to_v10.py` module, all `build_*_part` helpers, and
  the `meta_as_dict` / `meta_as_struct` wrappers respectively.
- **`a2akit.a2a.version` OTel span attribute** (§12).
- **`pytest-timeout`** in the dev extra.

### Changed
- **Internal types migrated to `a2a_pydantic.v10`** everywhere except the v0.3
  compat wire endpoints and the v0.3 client (both deliberate).
- **Event-pipeline terminal signalling** moved from the v0.3 `final=True` flag
  to a `TerminalMarker` wrapper. The v0.3 compat layer still emits
  `final=True` on the wire for legacy clients (§10).
- **AgentCard builder** now takes `protocol_versions=` and dispatches between
  v10 and v03.

### Removed
- **`a2a-sdk` is no longer a runtime dependency.** Kept in the `dev` group
  only — legacy tests still construct v0.3 types directly; those will be
  migrated in a follow-up sprint.
- **`TaskState.unknown`** references: v0.3 sentinel, v1.0 drops it.

### Breaking
- **Database schemas from pre-0.0.33 a2akit** cannot be read by the new code
  path — the JSON shape of `history` / `artifacts` columns changed. Drain
  pending tasks and wipe the `a2akit_*` tables before upgrading. Auto-
  migration is out of scope for this release (§3.4).
- **User workers that accessed `ctx.parts[i].root.text` directly** must
  migrate to `ctx.parts[i].text` (flat v10 Part). The idiomatic API
  (`ctx.user_text`, `ctx.files`, `ctx.data_parts`) is unaffected.
- **Tests that construct `a2a.types.Message` / `a2a.types.Task` and pass
  them straight to storage or `TaskManager`** will fail — storage expects
  `a2a_pydantic.v10` models now.

### Added (continued)
- **Native v1.0 wire endpoints (§5)**: `endpoints_v10.py` (REST, no `/v1/`
  prefix, wrapped SSE discriminator, `google.rpc.Status` errors) and
  `jsonrpc_v10.py` (PascalCase methods: `SendMessage`, `GetTask`,
  `CreateTaskPushNotificationConfig`, …). 11 integration tests.
- **`_errors_v10.py`** with exhaustive exception → descriptor catalog
  feeding REST, JSON-RPC, and (future) gRPC — one source of truth for
  HTTP/gRPC status / JSON-RPC code / reason string (§20).
- **Dual-protocol serving (§17)**: `A2AServer(protocol_version={"1.0", "0.3"})`
  mounts both router sets. REST distinguishes by path (v0.3 under `/v1/`,
  v1.0 at root); JSON-RPC uses a method-name dispatcher
  (`_dual_jsonrpc.py`) that routes PascalCase to v1.0 and slash-style to
  v0.3. Errors shape switches per request path so each client sees its
  native envelope. Agent Card advertises both. 7 integration tests.
- **Agent Card JWS signature verification (§19)**: `_signatures.py` with
  RFC 7515 detached JWS over RFC 8785 JCS canonicalization (excluding
  ``signatures[]`` from the hash). Wired into ``A2AClient(..., verify_signatures=)``
  with ``off`` / ``soft`` (default) / ``strict`` modes, trusted_keys
  allowlist, jku-host allowlist. 8 tests covering valid-signature,
  tampered-body, unknown-kid, jku-host enforcement.
- **New optional extra** ``pip install a2akit[signatures]`` pulls
  ``jwcrypto>=1.5`` and ``rfc8785>=0.1``.

### Deferred to follow-up releases
- Push model alignment (§7, low-value refactor), gRPC transport (§18),
  full test-suite migration (§14).

## [0.0.32] — 2026-04-08

### Fixed
- **Debug UI: completed state silently dropped in streaming chat** — the final
  `status-update` SSE event with `state="completed"` had no message text, causing
  the UI to skip the `onStatus` callback entirely. The chat message stayed on
  "working" instead of showing "completed". Now fires `onStatus` when state is
  present, even without text.
- **Debug UI: artifact text overwritten by intermediate status updates** — when a
  worker emitted `send_status()` between `emit_text_artifact()` calls, the
  accumulated artifact content was replaced with the transient status text.
  `onStatus` now preserves artifact text over status text.
- **Debug UI: protocol label showed "HTTP+JSON" for JSON-RPC agents** —
  case-sensitive comparison (`"JSONRPC" === "jsonrpc"`) failed for the enum value
  returned by the server. Now uses case-insensitive matching.

### Added
- **`tasks/list` on JSON-RPC transport** — previously only available on REST per
  spec v0.3 §3.5.6. Now exposed on JSON-RPC as well (spec v1.0 §9.4.4 added it
  officially). Required for Debug UI task listing to work in JSON-RPC mode.

## [0.0.31] — 2026-04-07

### Fixed
- **Debug UI: streaming status updates rendered as separate chat messages** —
  `handleStreaming()` in the React Debug UI called `addMsg("status", text)` for
  every SSE `status-update` event, creating individual badge elements in the chat
  instead of updating the existing agent message's state. Status updates now
  update the agent message in-place (text + state badge), matching the behavior
  of the non-streaming (blocking) handler.

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
