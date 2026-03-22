# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

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
