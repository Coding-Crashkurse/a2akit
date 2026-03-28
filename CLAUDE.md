# CLAUDE.md

This file provides guidance to AI coding agents when working with code in this repository.

## Project Overview

**a2akit** is an Agent-to-Agent (A2A) protocol framework for building agents with minimal boilerplate. It provides batteries-included support for streaming, cancellation, multi-turn conversations, artifact handling, push notifications, and pluggable backends (Memory, SQLite, PostgreSQL, Redis) — all on top of FastAPI.

- **PyPI**: `pip install a2akit`
- **Docs**: https://markuslang1987.github.io/a2akit/

## Prerequisites

- **Python:** 3.11, 3.12, or 3.13
- **uv:** Latest (Python package manager + runner)
- **Node.js:** >=20 (only for Debug UI development)
- **pre-commit:** Installed via `uv sync --dev`

## Common Commands

### Development Setup
```bash
uv sync --dev                                       # Install dev dependencies
uv sync --dev --extra postgres --extra redis         # Include optional backends
uv sync --dev --extra sqlite --extra postgres --extra redis  # All backends
pre-commit install                                   # Install git hooks
```

### Running Examples
```bash
uvicorn examples.echo.server:app --reload            # Echo server on :8000
uvicorn examples.echo.server:app --reload --port 8001  # Custom port
```

### Code Quality
```bash
uv run ruff format src/ tests/    # Format Python code (run FIRST)
uv run ruff check src/ tests/     # Lint Python code
uv run ruff check --fix src/ tests/  # Lint with auto-fix
uv run mypy src/                  # Type checking (strict mode)
```

### Testing
```bash
uv run pytest                                  # Full test suite with coverage (80% min)
uv run pytest -x -q --no-cov                  # Fast run, no coverage
uv run pytest -x -q --no-cov -m "not slow"    # Skip slow tests
uv run pytest tests/test_echo.py               # Single test file
uv run pytest tests/test_echo.py::test_name    # Single test function
```

**With backend services** (PostgreSQL, Redis):
```bash
A2AKIT_TEST_POSTGRES_URL="postgresql+asyncpg://test:test@localhost:5432/a2akit_test" \
A2AKIT_TEST_REDIS_URL="redis://localhost:6379/0" \
  uv run pytest
```

### Debug UI (React)
```bash
cd ui && npm install              # Install UI dependencies
cd ui && npm run build            # Compile TypeScript + Vite build
cd ui && npm run embed            # Embed built UI into src/a2akit/_static/
```

### Documentation
```bash
uv sync --group docs              # Install docs dependencies
uv run mkdocs serve               # Local preview on :8000
uv run mkdocs gh-deploy --force   # Deploy to GitHub Pages
```

### Building & Publishing
```bash
uv build                          # Build wheel + sdist
```

## Version Management & Releases

**Version is the single source of truth in `pyproject.toml`.**

Release workflow:
1. Update `version` in `pyproject.toml`
2. Update `CHANGELOG.md` with the new version's changes
3. Commit: `git commit -m "chore: bump version to X.Y.Z"`
4. Tag: `git tag vX.Y.Z` — the tag **must** match the `pyproject.toml` version exactly (CI verifies this)
5. Push: `git push origin main --tags`
6. CI runs tests, then publishes to PyPI via OIDC

## Architecture

### Repository Structure
```
src/a2akit/
├── server.py              # A2AServer — FastAPI app factory
├── endpoints.py           # REST + JSON-RPC route handlers
├── jsonrpc.py             # JSON-RPC protocol layer
├── task_manager.py        # Task lifecycle orchestration
├── config.py              # Pydantic Settings (env-based config)
├── dependencies.py        # Dependency injection container
├── errors.py              # Framework exceptions
├── hooks.py               # Lifecycle hooks system
├── event_emitter.py       # Event emission abstraction
├── agent_card.py          # A2A agent card configuration
├── worker/                # Worker base classes + TaskContext
├── broker/                # Task queue: Memory, Redis Streams
├── event_bus/             # Pub/sub: Memory, Redis Pub/Sub
├── storage/               # Persistence: Memory, SQLite, PostgreSQL
├── middleware/             # Auth (API key, Bearer token)
├── push/                  # Webhook push notifications
├── client/                # A2A client (JSON-RPC + REST transports)
├── telemetry/             # OpenTelemetry instrumentation
├── cancel.py              # Cancellation coordination
├── schema.py              # JSON schema utilities
├── _chat_ui.py            # Debug UI endpoint
└── _static/               # Embedded React debug UI bundle
ui/                        # Debug UI source (React 19 + Vite)
examples/                  # 20+ runnable examples
tests/                     # 70+ test files
docs/                      # MkDocs Material documentation
```

### Request Flow
```
HTTP → Middleware chain → JSON-RPC/REST endpoint → TaskManager
  → Storage (persist) + Broker (enqueue) + EventEmitter (notify)
  → Worker.handle(ctx) executes in background
  → EventBus publishes updates → SSE streams to client
```

### Key Design Patterns
- **Worker pattern**: Subclass `Worker`, implement `async handle(ctx: TaskContext)`
- **Pluggable backends**: Storage, Broker, EventBus each have Memory + Redis/SQL implementations
- **Dependency injection**: `DependencyContainer` manages shared resource lifecycle
- **Middleware pipeline**: Chain `A2AMiddleware` for auth, logging, validation
- **Async-first**: All I/O is async (FastAPI, anyio)
- **Optimistic concurrency control**: Version-tracked storage updates

## Pre-commit Hooks

Hooks run automatically on `git commit`:
1. **ruff-format** — Auto-format
2. **ruff check** — Lint with auto-fix (fails if changes were made)
3. **mypy** — Type check `src/` (strict mode)
4. **pytest** — Fast tests only (`-m "not slow"`, no coverage)

If a hook fails, fix the issue and re-commit.

## CI/CD

### `ci.yml` — Push to `main` + all PRs
- **Lint job**: ruff check + format verification
- **Test matrix**: Python 3.11/3.12/3.13 with PostgreSQL 16 + Redis 7

### `docs.yml` — Push to `main`
- Deploys MkDocs to GitHub Pages via `mkdocs gh-deploy`

### `publish.yml` — Git tag `v*`
- Verifies tag matches `pyproject.toml` version
- Runs full test suite
- Builds and publishes to PyPI via OIDC

## Code Style & Conventions

- **Formatter**: Ruff (double quotes, space indent, line length 99)
- **Type checking**: mypy strict mode with Pydantic plugin
- **Imports**: Absolute only (`ban-relative-imports = "all"`), isort via Ruff
- **Test coverage**: 80% minimum (enforced by pytest-cov in CI)
- **Async tests**: `asyncio_mode = "auto"` — no need for `@pytest.mark.asyncio`
- **Test markers**: `@pytest.mark.slow` for tests >2s (skipped in pre-commit)

## Documentation

Docs live in `docs/` and use MkDocs Material. Structure:
- `getting-started.md` — Installation + quick start
- `configuration.md` — All settings reference
- `concepts/` — Architecture, task lifecycle, worker model
- `guides/` — How-to guides (streaming, middleware, DI, hooks, cancellation, etc.)
- `examples/` — Walkthrough of example apps
- `reference/` — Auto-generated API docs via `mkdocstrings`

When adding new features, update the relevant docs and add an example if applicable.

## Pull Request Guidelines

- Follow [semantic commit conventions](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `chore:`, `perf:`, `docs:`, `test:`, `refactor:`)
- Ensure `ruff format`, `ruff check`, `mypy`, and `pytest` all pass
- Maintain or improve the 80% coverage threshold
- Reference issues when applicable (`Fixes #123`)
