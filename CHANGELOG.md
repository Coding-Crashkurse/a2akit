# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [0.0.24] — 2026-04-01

### Fixed
- **SSRF bypass via IPv6-mapped IPv4** — `_is_blocked_ip` now resolves
  `::ffff:10.x.x.x` to its mapped IPv4 address before checking blocked ranges,
  closing a webhook SSRF bypass.
- **`_enqueue_or_fail` publishes final SSE event** — broker failures now emit a
  `TaskStatusUpdateEvent(final=True)` via the event bus so blocking and streaming
  subscribers see the failure immediately instead of hanging until timeout.
- **Follow-up idempotency prevents re-enqueue** — `_submit_task` now returns a
  `should_enqueue` flag; duplicate follow-up messages (same `messageId`) no longer
  trigger a second broker enqueue / worker execution.
- **Cleanup chain resilience** — `event_bus.cleanup()` and
  `cancel_registry.cleanup()` in the worker's `finally` block are now individually
  wrapped in `try/except` so a failure in one does not skip the other.
- **`RedisCancelRegistry` connection leak** — `close()` is now called during
  server shutdown, preventing a leaked Redis connection on the cancel registry.
- **Redis idempotency key TTL** — `SET idem_key` now uses `EX 86400` (24 h) to
  prevent unbounded growth of idempotency keys in Redis.
- **Corrupt broker payloads moved to DLQ** — non-deserializable messages in
  `receive_task_operations` and `_claim_stale_messages` are now written to the
  dead-letter queue before being ACK'd, instead of vanishing silently.
- **`ConcurrencyError` exception handler** — added to both REST (`409 Conflict`)
  and JSON-RPC (`-32004`) so OCC conflicts return a proper error instead of 500.
- **Enum repr in error message** — `TaskNotAcceptingMessagesError` response now
  shows `working` instead of `TaskState.working`.
- **Client SSE read timeout** — streaming requests (`stream_message`,
  `subscribe_task`) now use `Timeout(5.0, read=None)` so long-running LLM
  agents don't trigger a `ReadTimeout` between chunks.
- **Redis EventBus safety poll throttle** — fallback XRANGE polls now fire every
  ~30 s instead of every 1 s per subscriber, reducing idle Redis load by ~97%.
- **Invalid `pageToken` handling** — non-numeric page tokens no longer crash with
  `ValueError` / 500; all three storage backends now fall back to offset 0.
- **Client `close()` resilience** — `transport.close()` failure no longer prevents
  `http_client.aclose()` from running (nested `try/finally`).

## [0.0.23] — 2026-03-31

### Fixed
- **Blocking timeout spec compliance** — `send_message` with `blocking=True` now
  returns the task in its current state on timeout instead of raising
  `UnsupportedOperationError` (HTTP 400), per A2A v0.3.0 §3.1.2.
- **Broker failure in blocking/streaming paths** — both `send_message` (blocking)
  and `stream_message` now wrap `broker.run_task` in `_enqueue_or_fail`, so a Redis
  outage marks the task as `failed` instead of leaving it stuck in `submitted`.
- **Follow-up message idempotency** — retried follow-up messages with the same
  `messageId` are now deduplicated against `task.history`, preventing duplicate
  appends and double-processing.
- **Middleware OTel span leak** — `after_dispatch` is now called in a `try/except`
  guard so `TracingMiddleware` always ends its span and detaches the OTel token,
  even when `send_message` raises.
- **Redis event bus SSE hang** — the live Pub/Sub loop now polls the Redis Stream
  on every timeout (1 s) as a fallback, preventing permanent SSE freezes when a
  Pub/Sub wakeup signal is lost over the network.
- **Redis task lock factory crash** — changed from `async def` to `def` so the
  factory returns a Lock (async context manager) directly instead of a coroutine,
  fixing `AttributeError: 'coroutine' object has no attribute '__aenter__'`.
- **Client SSE multi-line parsing** — the SSE parser now buffers `data:` lines and
  parses on empty-line boundaries per the W3C Server-Sent Events specification,
  fixing `JSONDecodeError` when connecting to servers that pretty-print JSON.
- **JSON-RPC client streaming error swallowed** — `stream_message` and
  `subscribe_task` now check the `Content-Type` header; a non-SSE JSON-RPC error
  response is properly raised as `ProtocolError` instead of yielding an empty stream.
- **Webhook delivery race conditions** — idle-timeout worker now checks
  `queue.empty()` before exiting; `_cleanup_queue` verifies worker identity to
  prevent a `call_soon`-scheduled callback from deleting a replacement worker's queue.
- **Redis broker poison pill** — `XAUTOCLAIM` now tracks hard-crash claims via a
  Redis Hash (`crash_counts`). Messages whose `base_attempt + claim_count` exceeds
  `max_retries` are moved to the DLQ, preventing an OOM-inducing message from
  cycling through the cluster indefinitely.
- **AnyIO cancellation cleanup** — worker `except Cancelled` and `finally` blocks
  now use `CancelScope(shield=True)` so that `_flush_artifacts`, `_mark_canceled`,
  and event-bus/cancel-registry cleanup actually execute during server shutdown
  instead of being immediately re-cancelled.
- **SQL pagination non-deterministic sort** — `list_tasks` now uses
  `(status_timestamp DESC, id DESC)` as sort key, preventing duplicate/missing
  tasks when multiple tasks share the same timestamp.
- **Stale status timestamp** — status-message-only updates (no state change) now
  update `status_timestamp` to the current time in all three storage backends,
  so polling clients see progress instead of a frozen timestamp.
- **FastAPI 422 → A2A error format** — `RequestValidationError` now returns
  `{"code": -32600, "message": "Invalid request parameters"}` instead of FastAPI's
  default `{"detail": [...]}` array.
- **Redis storage defensive deserialization** — empty `history` / `artifacts` hash
  fields (e.g. from manual tampering) now fall back to `"[]"` instead of crashing
  with `json.loads("")`.
- **Telemetry version** — `TRACER_VERSION` is now read dynamically from package
  metadata via `importlib.metadata.version("a2akit")`.
- **JSON-RPC `"params": null` crash** — `body.get("params", {})` replaced with
  `body.get("params") or {}` to handle explicit JSON `null`, preventing
  `AttributeError` in all JSON-RPC method handlers.
- **JSON-RPC auth error code** — `AuthenticationRequiredError` is now mapped to
  `-32600` with a descriptive message instead of falling through to `-32603
  Internal Error`.
- **`A2AClient` HTTP client leak** — `__aenter__` now calls `close()` if
  `connect()` fails, preventing the internally-created `httpx.AsyncClient` from
  leaking when the agent card fetch or validation raises.

## [0.0.22] — 2026-03-29

### Fixed
- **SSE Event-ID mismatch** — SSE endpoints now use event-bus-assigned IDs instead
  of a local counter, fixing `Last-Event-ID` reconnection replay.
- **JSON-RPC streaming error handling** — `message/sendStream` and
  `tasks/resubscribe` now eagerly evaluate the first event so validation errors
  produce proper JSON-RPC error responses instead of broken SSE streams.
- **Dependency shutdown order** — user-registered dependencies now shut down after
  the worker adapter exits, preventing crashes in workers that use deps during
  shutdown.
- **Push delivery semaphore scope** — the concurrency semaphore is now held only
  during the HTTP request, not during retry back-off sleeps.
- **Push delivery queue race condition** — `deliver()` now detects dead queue
  workers and restarts them instead of enqueueing into orphaned queues.
- **Agent message metadata leak** — agent response messages no longer inherit the
  user message's metadata.
- **Readiness endpoint resilience** — `health/ready` now catches exceptions from
  backend `health_check()` calls instead of returning HTTP 500.
- **AgentCard spec compliance** — `additionalInterfaces` no longer includes the
  primary interface; extended card endpoints now include `additional_protocols`.
- **InMemoryEventBus replay duplicates** — replay phase now tracks
  `last_yielded_id` to skip events already delivered via the live stream.
- **SQL storage stale OCC version** — `ConcurrencyError` now forces a fresh
  version read instead of reporting a stale value from the prior SELECT.
- **Redis cancel scope hang** — unexpected exceptions in the Pub/Sub listener now
  set the cancel event instead of leaving `wait()` blocked forever.
- **params.message mutation** — `send_message` and `stream_message` no longer
  mutate the caller's `MessageSendParams` object in-place.
- **cancel context_id** — cancel events now fall back to `task.context_id` when the
  caller-supplied context_id is None.
- **PushDeliveryEmitter shutdown** — background delivery trigger tasks are now
  cancelled during server shutdown.
- **`respond("")` empty parts** — empty string now correctly produces a TextPart
  instead of an empty parts list.
- **`request_auth(details="")` ignored** — empty string details are now included
  instead of being treated as None.
- **Redis Stream ID comparison** — removed redundant lexicographic string
  comparison that could theoretically fail for stream IDs of different lengths.
- **Artifact metadata default** — `emit_artifact` no longer forces `metadata: {}`
  when no metadata is provided.

## [0.0.21] — 2026-03-28

### Added
- **Redis storage backend** — new `RedisStorage` using Redis Hashes for tasks,
  Sets for context indexes, and Lua scripts for atomic OCC + terminal-state guards.
  Auto-detected when passing a `redis://` URL to the `storage` parameter.
  Install with `pip install a2akit[redis]`.
- **Push delivery queue idle timeout** — delivery queue workers now exit after
  a configurable idle timeout (default 300s) to prevent resource leaks when tasks
  stay in non-terminal states indefinitely. Configurable via `push_idle_timeout`
  constructor parameter or `A2AKIT_PUSH_IDLE_TIMEOUT` environment variable.

### Fixed
- **Redis CancelScope leak** — `cleanup()` now cancels listener tasks and closes
  pubsub subscriptions for the target task_id.

## [0.0.20] — 2026-03-28

### Added
- **Deploying to Production guide** — Docker Compose + Redis + PostgreSQL +
  Uvicorn workers + nginx reverse proxy configuration with production checklist.
- **Troubleshooting / FAQ page** — common errors (`TaskNotAcceptingMessagesError`,
  `ContentTypeNotSupportedError`, blocking timeout), stuck tasks, SSE drops,
  Redis issues, storage behavior, and debug UI setup.
- **TaskContext Quick Reference table** — scannable method/property overview at
  the top of the API reference page.

### Changed
- **Consistent REST error format** (Spec §3.2.3) — all error responses now use
  `{"code": <int>, "message": <string>}`. Removed `{"detail": {...}}` wrapper
  from `HTTPException` responses and JSON-RPC envelope from auth errors.
- **`TaskNotCancelableError` global handler** — cancel errors now go through the
  global exception handler instead of local `try/except` in the endpoint.
- **Blocking timeout raises error** (Spec §7.1.2) — `configuration.blocking`
  requests that exceed the timeout now return `UnsupportedOperationError` instead
  of silently returning a non-terminal task. Already-completed tasks are returned
  normally.
- **JSON-RPC `tasks/resubscribe` passes `lastEventId`** (Spec §3.4.1) — parity
  with the REST subscribe endpoint for event replay after reconnect.
- **Delete push config returns `200` with `null`** (Spec §7.8) — previously
  returned `204 No Content`.
- **HTTP webhook warning** — `push_allow_http=True` now logs a warning on every
  insecure webhook URL (A2A §4.1).

### Fixed
- **SQL storage `_trim_history`** — empty history list `[]` is no longer
  converted to `None`, matching `InMemoryStorage` behavior.
- **`ConcurrencyError` in InMemoryStorage** — now includes `current_version`
  in the exception, matching SQL backends.
- **Repository rename** — updated all `a2a-kit` references to `a2akit` in
  `mkdocs.yml` and `README.md`.
- **Echo example** — added `request_input()` demo flow (`"name"` command).

## [0.0.19] — 2026-03-28

### Changed
- **Deferred storage for streaming tasks** — when a client connects via
  `POST /v1/message:stream`, intermediate DB writes (`_maybe_flush`,
  `send_status`) are skipped entirely. SSE subscribers already receive every
  chunk in real-time via the EventBus; only the terminal write (`complete`,
  `fail`, etc.) persists the full state atomically.
  Streaming a 50-chunk task now produces **1 DB write instead of ~7–9**.
- **Redis EventBus: single-roundtrip publish** — `publish()` now pipelines
  `XADD` + `PUBLISH` into one Redis roundtrip. The Pub/Sub message is a
  lightweight wakeup signal; live subscribers read actual data via `XRANGE`,
  eliminating double serialization and halving per-event bandwidth.
- **Eliminated JSON double-serialization** — replaced 18 occurrences of
  `json.loads(obj.model_dump_json(...))` with `obj.model_dump(mode="json", ...)`
  across endpoints, JSON-RPC handler, storage, push delivery, and client
  transports. Removes one full JSON string allocation + parse per call.
- **`ConcurrencyError` carries `current_version`** — on version mismatch the
  already-loaded row version is attached to the exception, saving a separate
  `SELECT` on retry in both `_versioned_update` and `cancel_task_in_storage`.
- **Debug UI loaded from static files** — `_chat_ui.py` no longer embeds HTML
  inline; the built UI bundle is served from `_static/`.
- **Robust server shutdown** — `A2AServer` lifespan cleanup uses `hasattr`
  guard before deleting app state attributes, preventing `AttributeError` on
  partial startup failures.
- **EventEmitter docstrings** — expanded delivery-guarantee documentation for
  `DefaultEventEmitter` and `send_event()`.

### Fixed
- **Debug UI**: `preferredTransport` comparison is now case-insensitive.

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
- **`pip install a2akit[redis]`** — new optional dependency group
  (`redis[hiredis]>=5.0.0`).
- **12 new Redis settings** in `Settings` — `redis_url`, `redis_key_prefix`,
  `redis_broker_stream`, `redis_broker_group`, `redis_broker_block_ms`,
  `redis_broker_claim_timeout_ms`, `redis_event_bus_channel_prefix`,
  `redis_event_bus_stream_prefix`, `redis_event_bus_stream_maxlen`,
  `redis_cancel_key_prefix`, `redis_cancel_ttl_s`, and more.
- **Parametrized test fixtures** — `broker`, `event_bus`, and `cancel_registry`
  fixtures now run against both InMemory and Redis backends (Redis tests skip
  when `A2AKIT_TEST_REDIS_URL` is not set).
- **Redis-specific test suites** — `test_redis_broker.py`,
  `test_redis_event_bus.py`, `test_redis_cancel_registry.py`.
- **`examples/redis_langgraph/`** — full Docker Compose example with Redis +
  PostgreSQL + LangGraph research pipeline agent.

## [0.0.17] — 2026-03-22

### Added
- **Simultaneous Multi-Transport** (Spec §3.4, §5.5) — `A2AServer` now accepts
  `additional_protocols=["HTTP"]` (or `["JSONRPC"]`) to serve both JSON-RPC and
  REST transports in parallel. The agent card's `additionalInterfaces` is
  populated automatically. `examples/multi_transport/`.
- **Content-Type Request Validation** (Spec §3.2 MUST) — new
  `ContentTypeValidationMiddleware` rejects POST requests without
  `Content-Type: application/json` with HTTP 415. GET, DELETE, OPTIONS, HEAD,
  and discovery/health/chat paths are exempt.
- **`AuthenticationRequiredError` + `WWW-Authenticate` Header** (Spec §4.4
  SHOULD) — new exception type in `a2akit.errors`. The server exception handler
  returns HTTP 401 with `WWW-Authenticate: <scheme> realm="<realm>"`.
- **Built-in Auth Middlewares** (Spec §4.3–4.4):
  - `BearerTokenMiddleware` — validates `Authorization: Bearer <token>` via
    async verify callback. Claims available at `ctx.request_context["auth_claims"]`.
  - `ApiKeyMiddleware` — validates API keys from a configurable header
    (default `X-API-Key`). Key available at `ctx.request_context["api_key"]`.
  - Both raise `AuthenticationRequiredError` on failure and support
    `exclude_paths` for public routes.
  - `examples/auth_bearer/`, `examples/auth_apikey/`.
- **`request_auth()` with structured DataPart** (Spec §4.5 SHOULD) — new
  keyword arguments `schemes`, `credentials_hint`, `auth_url` on
  `TaskContext.request_auth()`. When provided, a `DataPart` with structured
  auth details is included alongside the optional text explanation.
  Backwards compatible — `request_auth("text")` still works as before.
- **Client: `last_event_id` on `subscribe()`** (Spec §7.9) — the client now
  passes `Last-Event-ID` as HTTP header when calling `subscribe(task_id,
  last_event_id="...")`, enabling SSE replay after reconnect. Both REST and
  JSON-RPC transports support this. `examples/subscribe_replay/`.
- **Client: Transport Fallback** (Spec §5.6.3 SHOULD) — `connect()` now builds
  a candidate list from the agent card's `preferredTransport` +
  `additionalInterfaces` and tries each with a health check. On connect failure,
  falls back to the next transport. New `health_check()` method on both
  transports. `examples/transport_fallback/`.
- **Client: Configurable Retries** — `A2AClient` now accepts `max_retries`,
  `retry_delay`, and `retry_on` parameters. Retries use exponential backoff.
  Applied to `send_parts`, `get_task`, `list_tasks`, `cancel`. NOT applied to
  streaming methods (use `subscribe()` + `last_event_id` for stream recovery).
  Default: `max_retries=0` (no retries, backwards compatible).

### Changed
- **Middleware restructured as package** — `a2akit.middleware` is now a package
  (`middleware/__init__.py`, `middleware/base.py`, `middleware/auth.py`) instead
  of a single file. All existing imports remain unchanged — no breaking change.
- `AuthenticationRequiredError`, `BearerTokenMiddleware`, `ApiKeyMiddleware`
  exported from `a2akit` top-level.

## [0.0.16] — 2026-03-21

### Added
- **A2A v0.3.0 Feature Completeness** — closes all remaining spec gaps:
  - **REQ-01: Message Field Passthrough** — `referenceTaskIds` and `extensions`
    on incoming `Message` objects are preserved through storage and exposed via
    `ctx.reference_task_ids` and `ctx.message_extensions` properties on `TaskContext`.
  - **REQ-02: Artifact Extensions** — `extensions` on `Artifact` objects are
    preserved through storage. `emit_artifact()` now accepts an
    `extensions: list[str] | None` parameter.
  - **REQ-03: Input Mode Validation** — when `defaultInputModes` is set on the
    agent card, the framework validates incoming message parts and returns
    `-32005 ContentTypeNotSupportedError` for incompatible MIME types.
    New `ContentTypeNotSupportedError` exception class.
  - **REQ-04: InvalidAgentResponseError** — new `-32006` error code and
    `InvalidAgentResponseError` exception, mapped in both REST and JSON-RPC
    transports.
  - **REQ-05: TaskState.Unknown** — `unknown` state is handled gracefully:
    not treated as terminal, accepts follow-up messages, transitions to
    `submitted` on new input.
  - **REQ-06: SSE Last-Event-ID Replay** — `InMemoryEventBus` now maintains
    a bounded per-task ring buffer of recent events with monotonic IDs.
    `subscribe(after_event_id=...)` replays buffered events. SSE frames
    include `id:` fields. New `Settings.event_replay_buffer` config (default: 100).
  - **REQ-08: Push Notification Inline Config on message/stream** —
    `stream_message()` now processes `configuration.pushNotificationConfig`
    identically to `send_message()`.
  - **REQ-09: A2A-Version Response Header** — all responses include
    `A2A-Version: 0.3.0` header.
  - **REQ-10: Discriminator Field Consistency** — verified `kind` fields
    on Task, Message, TaskStatusUpdateEvent, and TaskArtifactUpdateEvent
    are present in all serialized responses.
- Comprehensive test suite (`test_feature_completeness.py`) covering all 10 REQs.

## [0.0.15] — 2026-03-21

### Added
- **AgentCard Validator Hook** — optional `card_validator` parameter on `A2AClient`.
  - Accepts a `Callable[[AgentCard, bytes], None]` invoked during `connect()`, after
    the card is parsed but before the client accepts it.
  - Receives the parsed `AgentCard` and the raw HTTP response body (`bytes`) — the
    raw bytes are needed for JWS detached-payload verification where re-serialization
    would break the signature.
  - If the callable raises, `connect()` propagates the exception and the client stays
    disconnected.
  - `None` (default): no validation, behaviour identical to previous releases.
  - `examples/card_validator/` — server with JWS signature and three client validators
    (name allowlist, provider domain check, signature presence).
  - Comprehensive unit and integration tests.

## [0.0.14] — 2026-03-21

### Added
- **Authenticated Extended Card** (`agent/getAuthenticatedExtendedCard`) — A2A §5.5,
  §7.10, §9.1. Agents can now serve a richer agent card to authenticated callers.
  - New `extended_card_provider` parameter on `A2AServer` — async callback that
    receives the `Request` and returns an `AgentCardConfig`. When set,
    `supportsAuthenticatedExtendedCard` is automatically set to `True` on the
    public card.
  - REST: `GET /v1/card` returns the extended card (404 when not configured).
  - JSON-RPC: `agent/getAuthenticatedExtendedCard` method with error code `-32007`.
  - `A2AClient.get_extended_card()` — fetches the extended card via both transports.
  - `AgentCardConfig.supports_authenticated_extended_card` field (default `False`).
  - `examples/authenticated_card/` — server and client example.
  - Comprehensive tests (REST, JSON-RPC, client integration, config flags).

### Changed
- `CapabilitiesConfig` no longer raises `NotImplementedError` when
  `extended_agent_card=True`. The field is now accepted without error.

## [0.0.13] — 2026-03-20

### Added
- **AgentExtension support** — `CapabilitiesConfig` no longer raises
  `NotImplementedError` when `extensions` is set. Extensions are purely
  declarative and appear in the agent card under `capabilities.extensions`.
- **`required` field on `ExtensionConfig`** — mirrors `AgentExtension.required`
  from A2A v0.3.0 §5.5.2.1. Defaults to `False`; omitted from serialization
  when falsy.
- Reorganised examples into topic folders (`examples/<topic>/server.py` +
  `client.py`). Added missing client examples for middleware, hooks, otel,
  langgraph, output negotiation, dependency injection, and agent card topics.

### Changed
- `CapabilitiesConfig` docstring updated — extensions listed as supported.
- `_to_agent_extension()` now passes `required` through to `AgentExtension`.

## [0.0.12] — 2026-03-19

### Added
- **Push notification config CRUD** — Four new endpoints for managing webhook
  configurations per task (`set`, `get`, `list`, `delete`), available on both
  HTTP+JSON and JSON-RPC transports.
- **Webhook delivery engine** — Background service that POSTs task updates to
  client-provided webhook URLs on ALL state transitions. Includes
  exponential-backoff retries, sequential-per-config ordering, concurrent
  delivery limiting, and graceful shutdown.
- **URL validation (anti-SSRF)** — Webhook URLs are validated against private IP
  ranges, loopback addresses, and configurable allow/block lists before delivery.
- **A2AClient push methods** — `set_push_config()`, `get_push_config()`,
  `list_push_configs()`, `delete_push_config()`, plus `push_url`/`push_token`
  convenience parameters on `send()`.
- **InMemoryPushConfigStore** — In-memory storage backend for push configs.
- **PushDeliveryEmitter** — Emitter decorator that auto-triggers delivery on
  every state transition, stacking with HookableEmitter and TracingEmitter.
- **Security headers** — Webhook delivery includes `X-A2A-Notification-Token`
  and `Authorization` headers when configured by the client.
- **Configuration options** — New `A2AServer` parameters and env vars for retry,
  timeout, concurrency, and SSRF settings (`push_max_retries`, `push_retry_delay`,
  `push_timeout`, `push_max_concurrent`, `push_allow_http`).
- **Examples** — `examples/push/` (server, webhook receiver, client).

### Changed
- `CapabilitiesConfig` no longer raises `NotImplementedError` for
  `push_notifications=True`.
- Push notification endpoint stubs (previously returning 501) are now
  fully functional when `capabilities.push_notifications` is enabled.

## [0.0.11] — 2026-03-18

### Added
- **`ctx.accepts(mime_type)` — output mode negotiation** (A2A §7.1.2).
  - Workers can now check which output MIME types the client supports via
    `ctx.accepts("application/json")`, `ctx.accepts("text/csv")`, etc.
  - Returns `True` when the client listed the type in `acceptedOutputModes`,
    or when no filter was specified (absent or empty = accept everything).
  - Case-sensitive comparison per RFC 2045.
  - Threaded from `MessageSendConfiguration.acceptedOutputModes` through
    `ContextFactory` → `TaskContextImpl`.
  - `examples/output_negotiation/` — reference example with JSON/CSV/text fallback.
  - Unit and integration tests.

## [0.0.10] — 2026-03-16

### Added
- **AgentCard spec completeness** — all configurable A2A v0.3.0 Agent Card fields are now
  supported via `AgentCardConfig`.
  - `ProviderConfig` — declare the agent's provider (`organization`, `url`).
  - `icon_url` / `documentation_url` — optional metadata URLs.
  - `security_schemes` / `security` — declarative security scheme definitions
    (OpenAPI 3.0 Security Scheme Object). No enforcement — deklarativ only.
  - `SignatureConfig` — pass externally-generated JWS signatures (`protected`, `signature`,
    optional `header`). a2akit does not compute signatures.
- **Per-skill modes and security** on `SkillConfig`:
  - `input_modes` / `output_modes` — override global defaults per skill.
  - `security` — per-skill security requirements.
- `ProviderConfig` and `SignatureConfig` exported from `a2akit` top-level.
- `examples/agent_card/` — reference example with all new fields.
- Comprehensive unit and E2E tests for all new fields.

## [0.0.9] — 2026-03-15

### Added
- **State Transition History** — every task now records a chronological list of all
  state transitions in `task.metadata["stateTransitions"]`.
  - Each entry contains `state`, `timestamp`, and an optional `messageText` (extracted
    from the status message's first `TextPart`).
  - Transitions are always recorded regardless of the capability setting; the
    `state_transition_history` flag on `CapabilitiesConfig` only controls whether
    `capabilities.stateTransitionHistory` is advertised in the Agent Card.
  - `CapabilitiesConfig(state_transition_history=True)` to opt-in.
  - Works across all storage backends (InMemory, SQLite, PostgreSQL).
- **Debug UI: State Transitions Timeline** — the Task Dashboard detail view now shows
  a vertical timeline of all state transitions with state badges, timestamps, and
  optional message text.
- **Debug UI: Agent Info** — the sidebar now displays the `State History` capability
  (checkmark or cross).

## [0.0.8] — 2026-03-15

### Added
- **Built-in Debug UI** — browser-based interface for testing and inspecting A2A agents during development.
  - Activated via `server.as_fastapi_app(debug=True)`, served at `GET /chat`.
  - **Chat view** — send messages to the agent, see responses with state badges (`completed`, `failed`, `input-required`, etc.). Supports both blocking and streaming agents.
  - **Task Dashboard** — live-updating task list with configurable polling interval (0.5s–30s). Click any task to expand full details: history, artifacts, metadata.
  - Auto-discovers agent capabilities from `/.well-known/agent-card.json`.
  - Works with both JSON-RPC and HTTP+JSON protocols (auto-detected).
  - Agent info sidebar shows name, version, protocol, streaming support, modes, and skills.
  - Single self-contained HTML file (~220 KB), no additional Python dependencies.
  - Hidden from OpenAPI schema (`include_in_schema=False`).
  - Built with React + Vite, bundled as inline HTML via `vite-plugin-singlefile`.
  - `debug=False` (default): zero overhead, `/chat` not mounted.

## [0.0.7] — 2026-03-12

### Added
- **OpenTelemetry integration** — opt-in distributed tracing and metrics via `pip install a2akit[otel]`.
  - `TracingMiddleware` — creates root server spans per incoming A2A request with W3C context propagation.
  - `TracingEmitter` — adds span events for state transitions and records task metrics (duration, active count, errors).
  - Worker adapter instrumentation — wraps `_run_task_inner` with `a2akit.task.process` spans.
  - Client-side spans — `@traced_client_method` decorator on `send`, `connect`, `get_task`, `cancel`, `list_tasks`.
  - Context propagation — `traceparent` header injection in outgoing client requests for distributed tracing.
  - Semantic conventions in `a2akit.telemetry._semantic` with standardized span names, attribute keys, and metric names.
  - No-op fallback — zero overhead when OpenTelemetry is not installed.
  - Kill-switch — `OTEL_INSTRUMENTATION_A2AKIT_ENABLED=false` env-var to disable at runtime.
  - `enable_telemetry` parameter on `A2AServer` — `None` (auto-detect), `True` (force), `False` (disable).
  - Server-side metrics — `a2akit.task.duration`, `a2akit.task.active`, `a2akit.task.total`, `a2akit.task.errors`.
  - `examples/otel/` — reference example with console span exporter.
  - Comprehensive telemetry test suite.
- `OTEL_ENABLED` flag exported from `a2akit` top-level.

## [0.0.6] — 2026-03-12

### Added
- **A2AClient**: Dev-first client for interacting with A2A agents.
  - Auto-discovers agent capabilities from `/.well-known/agent-card.json`.
  - Auto-detects protocol (JSON-RPC or HTTP+JSON) from agent card.
  - `send()` for blocking/non-blocking message sending.
  - `stream()` for real-time streaming responses.
  - `send_parts()` for sending files, data, and mixed content.
  - `get_task()`, `list_tasks()`, `cancel()` for task management.
  - `subscribe()` for subscribing to existing task updates.
- **ClientResult**: Dev-friendly wrapper with `.text`, `.data`, `.artifacts` extraction.
- **StreamEvent**: Typed streaming event with `.kind`, `.text`, `.is_final`.
- **Client errors**: `AgentNotFoundError`, `AgentCapabilityError`, `NotConnectedError`,
  `TaskNotFoundError`, `TaskNotCancelableError`, `TaskTerminalError`, `ProtocolError`.
- Client integration tests for both HTTP+JSON and JSON-RPC protocols.
- **Client examples**: `examples/echo/client.py`, `examples/streaming/client.py`, and `examples/streaming/client_low_level.py`.
- **CapabilitiesConfig**: Explicit capability declaration for agents.
  - `streaming`: Enable/disable streaming support (default: `False`).
  - `push_notifications`: Placeholder, raises `NotImplementedError` when `True`.
  - `extended_agent_card`: Placeholder, raises `NotImplementedError` when `True`.
  - `extensions`: Placeholder, raises `NotImplementedError` when set.
- Server-side enforcement: unsupported streaming operations return `UnsupportedOperationError`.
- Client-side enforcement: `stream()`, `stream_text()`, and `subscribe()` check agent card before request.

### Changed
- **Breaking**: Streaming is now opt-in. Agents that use streaming must add
  `capabilities=CapabilitiesConfig(streaming=True)` to their `AgentCardConfig`.
  Previously all agents implicitly supported streaming.
- `AgentCardConfig` now uses a `capabilities` field (`CapabilitiesConfig`) instead of
  separate `streaming`, `push_notifications`, and `supports_extended_card` fields.

## [0.0.5] — 2026-03-10

### Added
- **JSON-RPC 2.0 protocol binding** — default A2A v0.3 transport.
  - Single `POST /` endpoint with full method dispatch (`message/send`, `message/sendStream`, `tasks/get`, `tasks/cancel`, `tasks/resubscribe`).
  - Push notification config stubs return `-32003 PushNotificationNotSupported`.
  - Standard JSON-RPC error codes (`-32700`, `-32600`, `-32601`, `-32602`, `-32603`) and A2A-specific codes (`-32001` – `-32006`).
  - SSE streaming with JSON-RPC envelope format (`data: {"jsonrpc":"2.0","id":...,"result":{...}}`).
  - Full middleware pipeline integration (same `A2AMiddleware` as REST).
  - Task sanitization (strip `_`-prefixed metadata) on all responses.
- `protocol` field on `AgentCardConfig` — `Literal["jsonrpc", "http+json"]`, defaults to `"jsonrpc"`.
- `validate_protocol()` function — rejects `"grpc"` and unknown values at construction time.
- Protocol-conditional router mounting in `A2AServer.as_fastapi_app()`.

### Changed
- REST (`http+json`) transport is now opt-in via `protocol="http+json"`.
- Agent card `url` and `preferred_transport` are derived from the configured protocol.

## [0.0.4] — 2026-03-07

### Added
- **PostgreSQL storage backend** via `PostgreSQLStorage` (connection string: `postgresql+asyncpg://...`).
- **SQLite storage backend** via `SQLiteStorage` (connection string: `sqlite+aiosqlite:///...`).
- MkDocs documentation site with Material theme.
- CI docs deployment workflow.

## [0.0.3] — 2026-03-07

### Changed
- Upgraded FastAPI dependency.
- Improved SSE endpoint robustness (setup dependencies for proper error handling).
- Pre-commit hook configuration.
- CI coverage threshold lowered to 80%.

## [0.0.2] — 2026-03-05

### Added
- **Lifecycle hooks** via `LifecycleHooks` dataclass and `HookableEmitter` decorator.
  - `on_state_change` — fires on every state transition (audit logs, debug tracing).
  - `on_working` — fires when a task starts or continues processing.
  - `on_turn_end` — fires when a task pauses for input (`input_required`, `auth_required`).
  - `on_terminal` — fires once when a task reaches a terminal state (`completed`, `failed`, `canceled`, `rejected`).
  - Hooks are fire-and-forget: errors are logged and swallowed, never affecting task processing.
  - Exactly-once guarantee for `on_terminal` via Storage terminal-state guard.
- `HookableEmitter` wraps any `EventEmitter` implementation — no changes to the ABC.
- `hooks` parameter on `A2AServer` for easy opt-in.
- `examples/hooks/` — reference example demonstrating all lifecycle hooks.
- **Middleware system** via `A2AMiddleware` base class and `RequestEnvelope` dataclass.
  - `before_dispatch` — runs before TaskManager processes the request (extract secrets, read headers, enrich context).
  - `after_dispatch` — runs after TaskManager returns (logging, metrics, cleanup). Reverse execution order.
  - `RequestEnvelope` separates persistent `params` from transient `context` — secrets never reach Storage.
  - `ctx.request_context` exposes transient middleware data inside workers.
  - Streaming endpoints run middleware in setup dependency; `after_dispatch` is skipped by design.
- `middlewares` parameter on `A2AServer` for easy registration.
- `examples/middleware/` — reference example demonstrating secret extraction middleware.

## [0.0.1] — 2025-XX-XX

### Added
- Initial release.
- `A2AServer` with one-liner FastAPI setup.
- `Worker` ABC with `TaskContext` for agent logic.
- Full A2A v0.3.0 HTTP+JSON transport (REST endpoints).
- Streaming via SSE (`message:stream`, `tasks:subscribe`).
- Cooperative and force-cancel with timeout fallback.
- Multi-turn support (`request_input`, `request_auth`).
- Direct reply mode (`reply_directly`).
- Artifact streaming with append semantics.
- Context persistence (`load_context`, `update_context`).
- `InMemoryStorage`, `InMemoryBroker`, `InMemoryEventBus`, `InMemoryCancelRegistry`.
- Agent card discovery (`/.well-known/agent-card.json`).
- Optimistic concurrency control on all storage writes.
- Idempotent task creation via `messageId`.
