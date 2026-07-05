# a2akit — Code Review Findings

**Date:** 2026-07-05 · **Revision reviewed:** `636f032` (feat: migrate to A2A v1.0 wire format) · **Scope:** all of `src/a2akit/` (~14,700 LOC) plus test-coverage analysis of `tests/` (77 files, 814 tests)

Five parallel deep reviews covered: task lifecycle, worker subsystem, protocol/HTTP layer, storage/broker/event-bus backends, and client/push/auth/telemetry. Every finding below was verified against surrounding code and callers before inclusion; several were reproduced empirically (the 204-body crash, the Timestamp `TypeError`, the `final=False` conversion warning).

---

## Executive summary

**The core engineering is genuinely strong.** The OCC retry ladders, subscribe-before-enqueue ordering, idempotency handling, artifact drain-into-terminal-write, and cooperative-vs-shutdown cancellation discrimination are careful, correct, and well-commented. This is not a codebase with systemic quality problems.

**The bugs cluster in three places:**

1. **The v0.3 → v1.0 migration seam** (the single biggest theme). The migration was executed by copy-paste-and-edit across parallel files (`jsonrpc.py`/`jsonrpc_v10.py`, `endpoints.py`/`endpoints_v10.py`), and the copies have drifted. Nearly half of all confirmed behavioral bugs — broken signature verification, dead telemetry metrics, `final: false` on terminal events, the 204-body crash, missing capability checks, `historyLength: 0` regression — are divergence artifacts between copies. The freshly rewritten v1.0 surface is also the least-tested part of the codebase (no tests at all for v1.0 SSE streaming, push configs, cancel, or readiness).
2. **The Redis broker's durability story.** Unbounded stream growth, a 60-second reclaim timeout that silently duplicates any long-running task in multi-worker deployments, and ~90 lines of recovery logic with zero test coverage. Not production-ready as shipped defaults.
3. **Cross-backend divergence.** The Memory/SQL/Redis conformance suites are good, but they don't probe the places where backends behave differently for the same call — and several of those divergences are real bugs (a client-reachable 500 on the default backend, wrong pagination counts on SQL).

**Overengineering verdict: mostly no.** The abstractions (EventEmitter chain, DependencyContainer, hooks, Worker ABC) each have real users and are minimal. The one significant structural problem is the ~600 lines of protocol-layer duplication, which is not speculative flexibility — it's transition debt that is actively producing bugs.

---

## 1. Critical

### C1. Agent-card JWS signature verification is silently broken for v1.0 cards
`src/a2akit/client/base.py:224-250`, `src/a2akit/client/base.py:88-106`

`connect()` verifies signatures on `self._agent_card`, which for v1.0 servers is the projection built by `_project_v10_card_to_v03` — and the projection never copies `signatures`. Result:

- **"soft" mode (the default):** signed v1.0 cards are *never* verified. A MITM can strip or alter the card undetected — exactly the attack §19 verification exists to catch.
- **"strict" mode:** `verify_agent_card` receives a signature-less card and raises "Agent Card has no signatures" even for correctly signed cards — strict mode is unusable against v1.0 servers.

Masked by tests: every client integration test sets `verify_signatures="off"` (`tests/test_v10_client_server.py:58,76`). **Fix:** verify against `self._card_v10` (which carries the signatures), and add an end-to-end signed-card test in both modes.

### C2. Default `jku` handling makes signature verification self-referential
`src/a2akit/_signatures.py:83-108`, `src/a2akit/client/base.py:140-141`

With the shipped defaults (`allow_jku_fetch=True`, `allowed_jku_hosts=None`), the verification key is fetched from a URL taken from the attacker-controlled JWS header. An attacker signs a forged card with their own key, points `jku` at their own JWKS, and verification "succeeds" — including in strict mode. The fetch is also an SSRF primitive (no private-IP check, unlike `push/validation.py`). **Fix:** default `jku` fetching off, or require an explicit host allowlist whenever `trusted_keys` is empty.

---

## 2. Major bugs

### Protocol / wire (mostly v1.0 migration drift)

| # | Finding | Location |
|---|---------|----------|
| M1 | v0.3 JSON-RPC SSE streams emit the terminal status event with `final: false` (spec violation; REST path fixed it at `endpoints.py:61-66`, JSON-RPC never got the fix). Reference-SDK clients keying off `final: true` never see stream end. | `jsonrpc.py:74-85, 384-391, 532-540` |
| M2 | v1.0 REST push-config DELETE returns 204 with a 4-byte `null` body → `RuntimeError` in uvicorn on every request (reproduced live). | `endpoints_v10.py:569-583` |
| M3 | v1.0 JSON-RPC push Get/List/Delete lack the push-capability check that Create and all v0.3 handlers have → `AttributeError` → `-32603` instead of `-32003` when push is disabled. | `jsonrpc_v10.py:609-670` |
| M4 | v1.0 `/health/ready` always returns HTTP 200 even when backends are down (v0.3 correctly returns 503). Kubernetes/LB readiness probes never fail. | `endpoints_v10.py:611-628` |
| M5 | JSON-RPC `params` is never validated as an object — array/string params crash the handler into a plain-text HTTP 500 instead of a `-32602`/`-32600` error envelope (both dispatchers). | `jsonrpc.py:225,409,468,499`; `jsonrpc_v10.py:183,417,486,505` |

### Client (v1.0 seam)

| # | Finding | Location |
|---|---------|----------|
| M6 | Task metrics compare against stale v0.3 state strings (`"working"`) while production passes v1.0 values (`"TASK_STATE_WORKING"`): the active gauge goes permanently negative, duration is never recorded, errors stay 0. Tests import the legacy enum, which is why this shipped. | `telemetry/_emitter.py:130-146` |
| M7 | v1.0 SSE parsers pass bare `Message` events to `StreamEvent.from_raw`, whose fallthrough assumes an artifact event → `AttributeError` kills the client stream. | `client/transport/rest_v10.py:365-368`, `jsonrpc_v10.py:290-304` |
| M8 | v1.0 SSE parsers never flush trailing `data_lines` at stream end — the *last* event (usually the terminal status, which v1.0 makes load-bearing) is dropped if the server closes without a trailing blank line. The v0.3 parser (`_sse.py:70-77`) handles this explicitly. | `client/transport/rest_v10.py:329-371`, `client/transport/jsonrpc_v10.py` |
| M9 | Synchronous `httpx.get` for the JWKS fetch blocks the entire event loop for up to 10 s inside async `connect()`. | `_signatures.py:95` |

### Worker & lifecycle

| # | Finding | Location |
|---|---------|----------|
| M10 | `_broker_loop` has no exception guard around the broker iterator. Anything other than `ConnectionError` from the Redis broker — including a pydantic `ValidationError` from one malformed/version-incompatible stream entry — kills task consumption *and* the server lifespan, and crashes again on every restart (consumer-level poison pill; the attempt-based poison-pill logic never applies because no `OperationHandle` exists yet). | `worker/adapter.py:95-99` |
| M11 | `deferred_storage` (on `return_immediately=True`) is inverted: it skips intermediate persistence for exactly the clients that poll `tasks/get` for progress, while blocking clients (who don't poll) get full persistence. Contradicts `TaskContext.send_status`'s own docstring. Encoded in tests as intended behavior → design bug. | `worker/adapter.py:299-308`, `worker/base.py:774, 1033` |
| M12 | `stream_message` never persists `params.tenant` into task metadata, while `send_message` does — tasks created via `message/stream` are invisible to `list_tasks(tenant=…)`. Same wire feature, divergent per transport. | `task_manager.py:524-561` vs `:411-421` |

### Backends

| # | Finding | Location |
|---|---------|----------|
| M13 | `list_tasks(status_timestamp_after=…)` raises `TypeError` on the **default (Memory) backend** — `Timestamp` wrapper compared to `str`. Client-reachable via `statusTimestampAfter` on all four endpoint surfaces → HTTP 500. SQL and Redis work. | `storage/memory.py:101-104` |
| M14 | Redis broker task stream and DLQ are never trimmed (no `maxlen`/`XTRIM` anywhere; XACK removes PEL entries, not stream entries; every nack XADDs a *new* entry). Unbounded Redis memory growth. | `broker/redis.py:517-520, 366-370` |
| M15 | Tasks running longer than `redis_broker_claim_timeout_ms` (default **60 s** — routine for LLM workloads) are XAUTOCLAIMed by another consumer and executed concurrently. No heartbeat; the per-task lock (`redis_task_lock_factory`) is opt-in. Violates the Broker ABC's own at-most-one contract (`broker/base.py:176-191`) in the default multi-worker config. | `broker/redis.py:599-676`, `config.py:32` |
| M16 | Blind writes (`expected_version=None`) under contention raise `ConcurrencyError` on SQL but are transparently retried on Redis (up to 8×) and can't conflict on Memory. The fix applied to Redis (`redis.py:394-396`) was never ported to `_sql_base.py` — spurious client-visible concurrency errors only on Postgres/SQLite. | `storage/_sql_base.py:350-360` |
| M17 | SQL tenant filter runs **after** pagination (Memory/Redis filter before): overstated `total_size`, sparse or empty pages that still carry `next_page_token` — clients treating an empty page as end-of-list silently miss tasks. | `storage/_sql_base.py:396-431` |

### Push (security)

| # | Finding | Location |
|---|---------|----------|
| M18 | Webhook SSRF check is TOCTOU-vulnerable to DNS rebinding: the IP is validated with one DNS resolution, then httpx re-resolves independently for the actual POST. Fix by pinning the validated IP (custom resolver/transport), or at minimum document `allowed_hosts` as the safe mode. | `push/validation.py:84-101`, `push/delivery.py:184-209` |

---

## 3. Minor bugs

Grouped; each is small but real.

**Protocol layer**
- `historyLength: 0` is dropped by `params.get("historyLength") or …` in the v1.0 dispatcher (0 is falsy) → full history returned. The v0.3 handler preserves 0 and has a test; v1.0 regressed. — `jsonrpc_v10.py:425, 465`
- DirectReply-as-first-event is skipped on some stream endpoints and serialized on others (reachable via `Last-Event-ID` replay) — wire behavior differs per endpoint for the same event. — `jsonrpc.py:383-385`, `endpoints.py:412/517`, `endpoints_v10.py:405/506`, `jsonrpc_v10.py:354-358/540-544`
- `contextlib.suppress(Exception)` around `convert_to_v03` silently sends v10-shaped JSON on the v0.3 wire if conversion fails, with no log. — `jsonrpc.py:79-82`
- `X-Forwarded-Proto`/`-Host` trusted unconditionally from any client → agent-card URL poisoning vector; honor forwarded headers only behind a trusted proxy. — `agent_card.py:380-386`
- Fallback INTERNAL_ERROR responses echo `str(exc)` of arbitrary exceptions to the client (may leak connection strings etc.). — `jsonrpc.py:128`, `_errors_v10.py:261, 294`
- `descriptor_for` uses exact-type lookup while the registered handlers use isinstance — user subclasses of framework errors map to 500 instead of their proper status. — `_errors_v10.py:177-179`
- 415 responses from `ContentTypeValidationMiddleware` are v0.3 JSON-RPC envelopes even on v1.0 servers. — `server.py:60-101`
- Constructor comment still advertises `protocol_version={"1.0","0.3"}` dual serving, which now raises `ValueError`. — `server.py:124-126` vs `_protocol.py:63-69`
- v1.0 cards drop configured `security_schemes` but still emit `security_requirements` referencing them → internally inconsistent card. — `agent_card.py:310-346`

**Lifecycle / worker**
- `_enqueue_or_fail` marks the task failed without a state guard or `expected_version` — an "enqueue failed but actually succeeded" broker error can stomp a live `working` task and discard the worker's real result. — `task_manager.py:180-221`
- The same handler logs the broker failure without `exc_info` — no traceback for diagnosis. — `task_manager.py:191`
- Canceled-then-terminal-without-dequeue tasks skip event-bus/cancel-registry cleanup → unbounded in-memory growth under persistent broker failure (Redis variants are TTL-bounded). — `task_manager.py:703-735`
- `stream_message`'s idempotent-duplicate path reloads without `history_length`, returning untrimmed history on retries. — `task_manager.py:568-571`
- Two concurrent `cancel_task` calls can both spawn 60 s force-cancel timers (benign, but the dedup comment overstates the guarantee). — `task_manager.py:674-683`
- `hooks.py` module docstring says "fire-and-forget" but hooks are awaited inline in the write path — a slow hook delays every client-facing state transition. One-line doc fix. — `hooks.py:1` vs `:75-79`
- Same `Dependency` registered under two keys gets `startup()`/`shutdown()` called twice (no identity dedup). — `dependencies.py:60-64, 83-88`
- `get_settings()` mutates global logging state inside a cached getter; invalid `A2AKIT_LOG_LEVEL` raises `ValueError` from whatever first touches settings. — `config.py:48-59`
- `run()`'s finally block awaits `broker.shutdown()` unshielded — hard lifespan cancellation skips broker shutdown (shield it like `adapter.py:440` does). — `worker/adapter.py:79-82`
- `_turn_ended = True` set only after event emission — a raising custom emitter leaves a persisted-terminal task flagged as unfinished (spurious error logs / redundant `_mark_failed`). — `worker/base.py:846-851`
- `send_status` broadcasts the `working` SSE event before the storage write discovers the task is terminal → replay buffer can record `working` after `canceled`. — `worker/base.py:1027-1028`
- `_drain_pending_artifacts` types `ctx` as `Any` and reaches into `ctx._pending_artifacts` — a rename would pass strict mypy and silently drain nothing on a correctness-critical path. — `worker/adapter.py:472-489`
- `push_store: Any = None` and the all-`getattr` `_extract_inline_push_config` defeat mypy strict. — `task_manager.py:163, 88-143`

**Backends**
- Redis idempotency keys expire after a hardcoded 24 h; SQL/Memory are permanent → duplicate task on >24 h retry, only on Redis. — `storage/redis.py:123`
- Terminal states hardcoded as string literals in the Lua script; the enum-derived `_TERMINAL_STATE_VALUES` is computed and never used — silent divergence if the enum ever changes. — `storage/redis.py:72-74, 128`
- `RedisStorage` crashes with `AttributeError` when given a user pool with `decode_responses=True` (blind `.decode()` in `load_task`/`update_task`/`list_tasks`; other paths check `isinstance(bytes)`). — `storage/redis.py:288, 406, 536`
- `not_before` backoff sleeps *inside* the consume generator (head-of-line blocking for all messages on that consumer) and is ignored entirely on the XAUTOCLAIM path. — `broker/redis.py:568-572, 616-676`
- `delete_task`/`delete_context` are non-atomic multi-step ops with racy return values (concurrent deletes both return `True`; crash mid-way orphans hashes), unlike the deliberately atomic `create_task`. — `storage/redis.py:574-628`
- InMemory `update_task` mutates stored history before artifact application (violates the ABC's all-or-nothing contract on partial failure) and stores caller object references (mutations leak into storage). — `storage/memory.py:248-255`
- SQLite sets no `busy_timeout` PRAGMA → concurrent writers get raw "database is locked" instead of typed errors. — `storage/sqlite.py:53-58`
- Memory vs Redis event-bus divergence: slow subscribers silently lose events only on Memory; the post-terminal replay window is 0 s on Memory vs 60 s on Redis; Memory's counter reset means a reused task_id with a stale `Last-Event-ID` suppresses real events. — `event_bus/memory.py:123-127, 219-224` vs `event_bus/redis.py:344-364`
- `redis_task_lock_factory` hardcodes `a2akit:tasklock:` and ignores `redis_key_prefix` — breaks the multi-app isolation every other key honors. — `broker/redis.py:709`
- SQL `create_task` re-reads via a second session post-commit and uses a bare `assert` for not-found (stripped under `python -O`). — `storage/_sql_base.py:255-256`

**Client / push / auth / telemetry**
- Stale-transport fallback bug: if `_create_transport` raises for candidate N, the loop can connect using candidate N−1's already-failed transport with mismatched protocol labels. — `client/base.py:281-307`
- Health-check 5xx fallback only matches the JSON-RPC transport's `ProtocolError("HTTP 5…")`; REST transports raise `httpx.HTTPStatusError` and are accepted as healthy. — `client/base.py:287-301`, `client/transport/rest.py:235-238`
- API-key check uses plain set membership rather than constant-time comparison; `Bearer` scheme match is case-sensitive (RFC 7235 says case-insensitive — fails closed, but interop-annoying). — `middleware/auth.py:112, 65`
- Server spans always end `StatusCode.OK`, including on errors — failed requests are indistinguishable in traces. — `telemetry/_middleware.py:130`
- `_detect_method` infers the A2A method by URL substring matching — task IDs containing "send"/"subscribe" are mislabeled; the routing layer already knows the real method. — `telemetry/_middleware.py:32-45`
- `_task_timers` grows without bound for tasks abandoned in `input-required` (moot until M6 is fixed, then real). — `telemetry/_emitter.py:44`
- Webhook URLs aren't validated at registration — SSRF-blocked URLs are accepted with 200 and silently dropped at delivery. — `push/endpoints.py:48-72`
- `_unwrap_nested` mutates the caller's input dict via `pop` in a before-validator. — `push/models.py:64-81`
- Per-config push delivery queues are unbounded (full Task snapshots) — a blackholing webhook accumulates memory. — `push/delivery.py:129`
- `task_id`/`config_id` interpolated into URL paths without percent-encoding (`../card` changes the request target). — `client/transport/rest.py:137` and siblings

---

## 4. Overengineering & duplication

The headline: **this codebase is not overengineered in the classic sense** — no speculative abstraction layers, no config nobody asked for. The structural debt is *duplication*, and it is measurably producing bugs.

1. **The v0.3/v1.0 protocol copies** (`jsonrpc.py` 729 lines vs `jsonrpc_v10.py` 726 lines; same for endpoints). The dispatcher core, SSE generator skeletons, middleware rollback orchestration, and push/health handlers are structurally identical with ~3 genuine variation points (method names, event encoding, error envelope). Findings M1, M3, M5, and the `historyLength: 0` regression are all copy-drift bugs. If v0.3 is scheduled for deletion, this is acceptable transition debt; if both live on, extract a shared dispatcher parameterized by (dispatch table, error builder, event encoder). — `jsonrpc.py` / `jsonrpc_v10.py`
2. **Same-version copy-paste inside the v1.0 layer**: `_sanitize_task_for_client_v10` is verbatim-duplicated between REST and JSON-RPC, and the two ~50-line `_wrap_*_stream_event_v10` copies differ only in returning str vs dict. Not transition debt — just duplication. — `endpoints_v10.py:55-116` vs `jsonrpc_v10.py:73-80, 380-413`
3. **Triplicated terminal-write retry logic**: `_mark_failed` (adapter), `cancel_task_in_storage` (cancel.py), and `_versioned_update` share the same get-version → guarded-write → reload/retry → artifact-fallback ladder, already drifting subtly. A shared `_terminal_write(state, reason, …)` would halve it. Also: `_mark_canceled` is a do-nothing forwarder whose parameter order is *inverted* relative to `_mark_failed` — a swapped-args trap. — `worker/adapter.py:491-595`, `cancel.py:27-134`
4. **Triplicated v0.3→v10 coercion preamble** pasted into every backend's `create_task`/`update_task` — coerce once in the base class. — `storage/memory.py:222-231`, `_sql_base.py:275-284`, `redis.py:380-389`
5. **Dead code / migration leftovers**: `mounted_rest`/`mounted_jsonrpc` sets written but never read (`server.py:462-493`); a ternary with two identical branches (`agent_card.py:273`); `_check_streaming` computes a descriptor and discards it, while several v1.0 handlers hardcode error codes bypassing the central catalog built to prevent exactly that (`jsonrpc_v10.py:284-299, 429-436, 577-579, 681-704`); one-line pass-through wrappers `_extract_files`/`_extract_data_parts` (`worker/base.py:93-104`); unreachable DLQ branch in `RedisOperationHandle.nack` (`broker/redis.py:346-389`); `AsyncExitStack` for a single context manager (`worker/adapter.py:227-232`); constructor mutates the caller's `agent_card` object (`server.py:192-193`).
6. **`worker/base.py` at 1,198 lines is mostly fine** — ~540 lines are the user-facing `TaskContext` ABC docstrings that feed the reference docs. The cost is every member being declared twice (ABC + sole Impl). Don't grow this pattern; split ABC/Impl into two modules if it must grow.

---

## 5. Test-coverage gaps

814 tests is a real suite, and the storage conformance mirroring (40 tests × 4 backends) is genuinely good. The gaps are concentrated and follow the bugs exactly:

**Highest value (these gaps are why the majors above shipped):**

| Gap | Would have caught |
|-----|-------------------|
| **v1.0 SSE streaming — zero tests** across all four surfaces (`SendStreamingMessage`, `SubscribeToTask`, `message:stream`, `tasks:subscribe`); both `_wrap_*_stream_event_v10` copies never execute in any test | M7, M8, DirectReply divergence |
| **v1.0 push-config endpoints — zero tests** (REST + JSON-RPC) | M2, M3 |
| **Signature verification through `connect()`** — unit tests cover `_signatures.py` in isolation; every integration test sets `verify_signatures="off"` | C1, C2, M9 |
| **Telemetry metrics** — tests import the legacy v0.3 enum and assert no instrument values at all | M6 |
| **Redis broker durability surface** — XAUTOCLAIM reclaim, poison-pill DLQ, `not_before` delayed retry: ~90 lines of the most intricate recovery logic, zero coverage (6 broker tests total) | M10, M14, M15 |
| **`statusTimestampAfter` + tenant filter on `list_tasks`** — zero tests on any backend | M13, M17, M12 |
| **OCC contention paths** — Redis blind-write retry loop (incl. 8-attempt exhaustion), SQL rowcount-0 branch, `update_context` upsert race: only simple match/mismatch is tested, despite the last two releases being OCC-race fixes | M16 |

**Also missing:** hard-cancel arriving mid-`handle()` (the cancel-watcher → drain-artifacts-into-canceled-write path — the most intricate code in the adapter, zero direct coverage); the shutdown-interruption redelivery guarantee (a regression would turn every deploy into task cancellations); turn-lifecycle guards (double `complete()`, `send_status` after turn end); blocking-wait timeout branch; v1.0 cancel/readiness/extended-card/notifications; `final: true` assertion on v0.3 JSON-RPC streams; push 5xx retry/backoff and per-config ordering (the stated design property of the delivery queue).

---

## 6. Recommended action plan

**Now (correctness/security, small diffs):**
1. C1 + C2 — fix signature verification for v1.0 cards and the `jku` trust default; add the end-to-end signed-card test.
2. M13 — one-line Timestamp fix in Memory `list_tasks` (+ a cross-backend filter test).
3. M10 — catch-log-continue guard around `_broker_loop`.
4. M6 — update telemetry state strings to v1.0 values (+ a real metrics test).
5. M2, M3, M4, M1 — the four small protocol-drift fixes.
6. M7, M8 — v1.0 SSE parser crash + trailing-event flush.

**Next (design decisions):**
7. M15 + M14 — Redis broker: raise the claim-timeout default substantially (or add a heartbeat), make the task lock the default in multi-worker configs, and add `maxlen`/XTRIM to the streams. Until then, document Redis broker as beta.
8. M11 — decide the intended `deferred_storage` semantics; current behavior contradicts the docstring for exactly the polling clients it affects.
9. M16 + M17 — port the blind-write retry to `_sql_base`, move the tenant filter before pagination (needs a column or JSON-path filter).
10. M12 — persist tenant on the stream path (one block copied from `send_message`).

**Then (structure, pays down the bug source):**
11. Backfill the seven high-value test gaps above *before* refactoring — they are the safety net for step 12.
12. Decide the v0.3 story. If it's leaving: schedule deletion and accept the duplication until then. If it's staying: extract the shared dispatcher/encoder core. Either way, dedupe the *same-version* copies (`_sanitize`, `_wrap_stream_event`) now — that's not transition debt.
13. Consolidate the terminal-write retry ladder and the backend coercion preamble; sweep the dead code list in §4.5.

---

*Generated by a five-agent parallel review (task lifecycle, worker, protocol, backends, client/push/auth/telemetry) with per-finding verification against callers. Line numbers refer to revision `636f032`.*
