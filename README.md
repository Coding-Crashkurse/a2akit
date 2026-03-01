# a2akit

[![PyPI](https://img.shields.io/pypi/v/a2akit)](https://pypi.org/project/a2akit/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/a2akit)](https://pypi.org/project/a2akit/)
[![CI](https://github.com/Coding-Crashkurse/a2a-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/Coding-Crashkurse/a2a-kit/actions)

**A2A agent framework in one import.**

Build [Agent-to-Agent (A2A)](https://google.github.io/A2A/) protocol
agents with minimal boilerplate. Streaming, cancellation, multi-turn
conversations, and artifact handling — batteries included.

## Install

```bash
pip install a2akit
```

With optional LangGraph support:

```bash
pip install a2akit[langgraph]
```

## Quick Start

```python
from a2akit import A2AServer, AgentCardConfig, TaskContext, Worker


class EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


server = A2AServer(
    worker=EchoWorker(),
    agent_card=AgentCardConfig(
        name="Echo Agent",
        description="Echoes your input back.",
        version="0.1.0",
    ),
)
app = server.as_fastapi_app()
```

Run it:

```bash
uvicorn my_agent:app --reload
```

Test it:

```bash
curl -X POST http://localhost:8000/v1/message:send \
  -H "Content-Type: application/json" \
  -d '{"message":{"role":"user","parts":[{"text":"hello"}],"messageId":"1"}}'
```

## Features

- **One-liner setup** — `A2AServer` wires storage, broker, event bus, and endpoints
- **Streaming** — word-by-word artifact streaming via SSE
- **Cancellation** — cooperative and force-cancel with timeout fallback
- **Multi-turn** — `request_input()` / `request_auth()` for conversational flows
- **Direct reply** — `reply_directly()` for simple request/response without task tracking
- **Pluggable backends** — swap in Redis, PostgreSQL, RabbitMQ (coming soon)
- **Type-safe** — full type hints, `py.typed` marker, PEP 561 compliant

## Architecture

a2akit allows you to bring your own `Storage`, `Broker`, `EventBus`,
`CancelRegistry` and `Worker`. You can also leverage the in-memory implementations
for development.

```mermaid
graph TD
    HTTP["HTTP Server\n(FastAPI + SSE)"]
    TM["TaskManager\n(coordinates)"]
    WA["WorkerAdapter\n(lifecycle)"]
    EE["EventEmitter\n(facade)"]
    B["Broker\n(queues & schedules)"]
    S["Storage\n(persistence)"]
    EB["EventBus\n(event fan-out)"]
    CR["CancelRegistry\n(cancel signals)"]
    W["Worker\n(your code)"]
    TC["TaskContext\n(execution API)"]

    HTTP -->|requests / responses| TM
    TM -->|schedules tasks| B
    TM -.->|reads / writes| S
    TM -.->|subscribes| EB
    TM -.->|cancel signals| CR
    B -->|delegates execution| WA
    WA -->|"handle(ctx)"| W
    W -.->|uses API| TC
    TC -->|emits via| EE
    EE -->|writes| S
    EE -->|publishes| EB
    WA -.->|checks / cleanup| CR
```

**TaskManager** handles submission, validation, streaming, and cancellation. It coordinates
between Broker, Storage, EventBus, and CancelRegistry — but never touches the Worker
directly.

**WorkerAdapter** bridges the Broker queue to your Worker. It manages the lifecycle:
dequeue -> check cancel -> build context -> transition to `working` -> call `handle(ctx)` ->
cleanup.

**EventEmitter** is the facade that TaskContext uses to persist state (Storage) and broadcast
events (EventBus) without knowing about either directly. Storage writes are authoritative;
EventBus is best-effort.

**Pluggable backends:** Swap `Storage`, `Broker`, `EventBus`, and `CancelRegistry`
independently — e.g. PostgreSQL storage + Redis broker + Redis event bus. All backends
implement their respective ABC.

## TaskContext API

`TaskContext` is the only interface your worker needs. It abstracts away all A2A protocol details:

### Properties

| Property | Description |
|---|---|
| `ctx.user_text` | The user's input as plain text |
| `ctx.parts` | Raw message parts (text, files, etc.) |
| `ctx.files` | File parts as `list[FileInfo]` (content, url, filename, media_type) |
| `ctx.data_parts` | Structured data parts as `list[dict]` |
| `ctx.task_id` | Current task UUID |
| `ctx.context_id` | Conversation / context identifier |
| `ctx.message_id` | ID of the triggering message |
| `ctx.metadata` | Arbitrary metadata from the request |
| `ctx.is_cancelled` | Check if cancellation was requested |
| `ctx.turn_ended` | Whether a terminal method was called |
| `ctx.history` | Previous messages in this task (`list[HistoryMessage]`) |
| `ctx.previous_artifacts` | Artifacts from prior turns (`list[PreviousArtifact]`) |

### Lifecycle

| Method | Description |
|---|---|
| `ctx.complete(text?)` | Mark task completed with optional text artifact |
| `ctx.complete_json(data)` | Complete with a JSON data artifact |
| `ctx.respond(text?)` | Complete with a direct message (no artifact) |
| `ctx.reply_directly(text)` | Return a Message directly without task tracking |
| `ctx.fail(reason)` | Mark task failed |
| `ctx.reject(reason?)` | Reject the task |
| `ctx.request_input(question)` | Ask user for more input |
| `ctx.request_auth(details?)` | Request secondary authentication |

### Streaming

| Method | Description |
|---|---|
| `ctx.send_status(msg)` | Emit intermediate status update |
| `ctx.emit_text_artifact(...)` | Emit a text artifact chunk |
| `ctx.emit_data_artifact(data)` | Emit a structured data artifact chunk |
| `ctx.emit_artifact(...)` | Emit an artifact with any content (text, data, file_bytes, file_url) |

### Context

| Method | Description |
|---|---|
| `ctx.load_context()` | Load stored context for this conversation |
| `ctx.update_context(data)` | Store context for this conversation |

No `Part(root=TextPart(...))`. No `EventQueue`. No `TaskUpdater`. Just call methods.

## Streaming Example

```python
import asyncio
from a2akit import A2AServer, AgentCardConfig, TaskContext, Worker


class StreamingWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        words = ctx.user_text.split()
        await ctx.send_status(f"Streaming {len(words)} words...")

        for i, word in enumerate(words):
            is_last = i == len(words) - 1
            await ctx.emit_text_artifact(
                text=word + ("" if is_last else " "),
                artifact_id="stream",
                append=(i > 0),
                last_chunk=is_last,
            )
            await asyncio.sleep(0.1)

        await ctx.complete()


server = A2AServer(
    worker=StreamingWorker(),
    agent_card=AgentCardConfig(
        name="Streamer",
        description="Word-by-word streaming",
        version="0.1.0",
    ),
)
app = server.as_fastapi_app()
```

## A2AServer Configuration

```python
server = A2AServer(
    worker=MyWorker(),
    agent_card=AgentCardConfig(
        name="My Agent",
        description="What your agent does.",
        version="0.1.0",
    ),
    storage="memory",             # or pass a Storage instance
    broker="memory",              # or pass a Broker instance
    event_bus="memory",           # or pass an EventBus instance
    cancel_registry=None,         # or pass a CancelRegistry instance
    blocking_timeout_s=30.0,      # timeout for blocking requests
    max_concurrent_tasks=None,    # limit parallel task execution
)
app = server.as_fastapi_app()
```

## Endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/v1/message:send` | Submit a message, return task or direct reply |
| POST | `/v1/message:stream` | Submit a message, stream events via SSE |
| GET | `/v1/tasks/{task_id}` | Get a single task by ID |
| GET | `/v1/tasks` | List tasks with filters and pagination |
| POST | `/v1/tasks/{task_id}:cancel` | Cancel a task |
| POST | `/v1/tasks/{task_id}:subscribe` | Subscribe to task updates via SSE |
| GET | `/v1/health` | Health check |
| GET | `/.well-known/agent-card.json` | Agent discovery card |

## A2A Protocol Version

a2akit implements [A2A v0.3.0](https://google.github.io/A2A/).

## Roadmap

Planned features for upcoming releases. Priorities may shift based on feedback.

| Feature | Target |
|---|---|
| Task middleware / hooks | v0.1.0 |
| Lifecycle hooks + dependency injection | v0.1.0 |
| Documentation website | v0.1.0 |
| Redis EventBus | v0.2.0 |
| Redis Broker | v0.2.0 |
| PostgreSQL Storage | v0.2.0 |
| SQLite Storage | v0.2.0 |
| Backend conformance test suite | v0.2.0 |
| OpenTelemetry integration | v0.2.0 |
| RabbitMQ Broker | v0.3.0+ |
| JSON-RPC transport | v0.3.0+ |
| gRPC transport | v0.4.0+ |

This roadmap is subject to change.

## License

MIT
