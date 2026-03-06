# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [0.0.2] — 2026-03-06

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
- `examples/hook_printer.py` — reference example demonstrating all lifecycle hooks.

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
