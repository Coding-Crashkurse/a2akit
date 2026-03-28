# a2akit

[![PyPI](https://img.shields.io/pypi/v/a2akit)](https://pypi.org/project/a2akit/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/a2akit)](https://pypi.org/project/a2akit/)
[![CI](https://github.com/Coding-Crashkurse/a2akit/actions/workflows/ci.yml/badge.svg)](https://github.com/Coding-Crashkurse/a2akit/actions)
[![Coverage](https://img.shields.io/badge/coverage-80%25+-brightgreen)](https://github.com/Coding-Crashkurse/a2akit)

**Production-grade [A2A protocol](https://google.github.io/A2A/) framework for Python.**

Build Agent-to-Agent agents with streaming, cancellation, multi-turn conversations, push notifications, pluggable backends (Memory, SQLite, PostgreSQL, Redis), OpenTelemetry, and a built-in debug UI — all on top of FastAPI.

## Why a2akit?

The [official A2A Python SDK](https://github.com/a2aproject/a2a-python) gives you protocol primitives — but you have to wire everything yourself. You need to understand `AgentExecutor`, `RequestContext`, `EventQueue`, `TaskUpdater`, `TaskState`, `Part`, `TextPart`, `SendMessageRequest`, task creation, event routing, and more. A minimal agent easily becomes 50+ lines of boilerplate:

<details>
<summary><b>Official SDK — Currency Agent (simplified)</b></summary>

```python
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import InternalError, InvalidParamsError, Part, TaskState, TextPart, UnsupportedOperationError
from a2a.utils import new_agent_text_message, new_task
from a2a.utils.errors import ServerError

class CurrencyAgentExecutor(AgentExecutor):
    def __init__(self):
        self.agent = CurrencyAgent()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        query = context.get_user_input()
        task = context.current_task
        if not task:
            task = new_task(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        try:
            async for item in self.agent.stream(query, task.context_id):
                if not item["is_task_complete"] and not item["require_user_input"]:
                    await updater.update_status(
                        TaskState.working,
                        new_agent_text_message(item["content"], task.context_id, task.id),
                    )
                elif item["require_user_input"]:
                    await updater.update_status(
                        TaskState.input_required,
                        new_agent_text_message(item["content"], task.context_id, task.id),
                        final=True,
                    )
                    break
                else:
                    await updater.add_artifact(
                        [Part(root=TextPart(text=item["content"]))], name="conversion_result"
                    )
                    await updater.complete()
                    break
        except Exception as e:
            raise ServerError(error=InternalError()) from e

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise ServerError(error=UnsupportedOperationError())
```

</details>

**With a2akit, the same logic is just this:**

```python
from a2akit import Worker, TaskContext

class CurrencyWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        async for item in my_agent.stream(ctx.user_text, ctx.task_id):
            if item["require_user_input"]:
                await ctx.request_input(item["content"])
            elif item["is_task_complete"]:
                await ctx.complete(item["content"])
            else:
                await ctx.send_status(item["content"])
```

**The difference:** a2akit handles task creation, event routing, state machines, error wrapping, SSE streaming, and protocol compliance for you. You write your agent logic — the framework handles the protocol.

| | Official SDK | a2akit |
|---|---|---|
| **Boilerplate** | Manage `EventQueue`, `TaskUpdater`, `TaskState`, `Part` objects manually | `ctx.complete()`, `ctx.send_status()`, `ctx.request_input()` |
| **Task lifecycle** | Create tasks, track state, wire events yourself | Automatic — framework manages the full lifecycle |
| **Streaming** | Manual SSE + event queue plumbing | Built-in, one method call |
| **Storage** | Bring your own | Memory, SQLite, PostgreSQL, Redis out of the box |
| **Cancellation** | Implement yourself | Cooperative + force-cancel with timeout |
| **Push notifications** | Implement yourself | Built-in with anti-SSRF and retries |
| **Debug UI** | None | Built-in browser UI at `/chat` |
| **Middleware** | Implement yourself | Pluggable pipeline (auth, validation, etc.) |

## Install

```bash
pip install a2akit
```

## Quick Start — Echo Agent in 8 Lines

```python
from a2akit import A2AServer, AgentCardConfig, TaskContext, Worker

class EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")

server = A2AServer(
    worker=EchoWorker(),
    agent_card=AgentCardConfig(name="Echo", description="Echoes input back.", version="0.1.0"),
)
app = server.as_fastapi_app(debug=True)
```

```bash
uvicorn my_agent:app --reload
# Agent running at http://localhost:8000
# Debug UI at http://localhost:8000/chat
```

## Quick Start — OpenAI Agent in 15 Lines

```python
from openai import AsyncOpenAI
from a2akit import A2AServer, AgentCardConfig, TaskContext, Worker

client = AsyncOpenAI()

class OpenAIWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": ctx.user_text}],
        )
        await ctx.complete(response.choices[0].message.content)

server = A2AServer(
    worker=OpenAIWorker(),
    agent_card=AgentCardConfig(name="GPT Agent", description="OpenAI-powered agent.", version="0.1.0"),
)
app = server.as_fastapi_app(debug=True)
```

Any LLM SDK works — the Worker pattern is framework-agnostic.

## Client

```python
from a2akit import A2AClient

async with A2AClient("http://localhost:8000") as client:
    result = await client.send("Hello, agent!")
    print(result.text)

    # Streaming
    async for chunk in client.stream_text("Stream me"):
        print(chunk, end="")
```

## Debug UI

```python
app = server.as_fastapi_app(debug=True)
```

Open `http://localhost:8000/chat` — chat with your agent, inspect tasks, and view state transitions in real time.

![Debug UI](docs/images/img1.png)

## Architecture

```
HTTP Request
  |
  v
Middleware chain (auth, validation, content-type)
  |
  v
JSON-RPC / REST endpoint
  |
  v
TaskManager (lifecycle orchestration)
  |
  +---> Storage (persist)     -- Memory | SQLite | PostgreSQL
  +---> Broker (enqueue)      -- Memory | Redis Streams
  +---> EventEmitter (notify) -- Hooks | Tracing | Push
  |
  v
Worker.handle(ctx: TaskContext)   <-- your code here
  |
  +---> ctx.complete(text)        -- finish the task
  +---> ctx.send_status(text)     -- progress update
  +---> ctx.emit_text_artifact()  -- streaming chunks
  +---> ctx.request_input(text)   -- multi-turn
  +---> ctx.fail(text)            -- error
  |
  v
EventBus (fan-out)  -- Memory | Redis Pub/Sub + Streams
  |
  v
SSE stream to client
```

## Extras

```bash
pip install a2akit[redis]       # Redis broker, event bus & cancel registry
pip install a2akit[postgres]    # PostgreSQL storage
pip install a2akit[sqlite]      # SQLite storage
pip install a2akit[langgraph]   # LangGraph integration
pip install a2akit[otel]        # OpenTelemetry tracing & metrics
```

## All Features

- **One-liner setup** — `A2AServer` wires storage, broker, event bus, and endpoints
- **A2AClient** — auto-discovers agents, supports send/stream/cancel/subscribe with retries and transport fallback
- **Streaming** — word-by-word artifact streaming via SSE
- **Cancellation** — cooperative and force-cancel with timeout fallback
- **Multi-turn** — `request_input()` / `request_auth()` for conversational flows
- **Direct reply** — `reply_directly()` for simple request/response without task tracking
- **Multi-transport** — JSON-RPC and HTTP+JSON simultaneously
- **Middleware pipeline** — auth extraction (Bearer, API key), header injection, payload sanitization
- **Push notifications** — webhook delivery with anti-SSRF validation and configurable retries
- **Lifecycle hooks** — fire-and-forget callbacks on state transitions
- **Dependency injection** — shared infrastructure with automatic lifecycle management
- **OpenTelemetry** — distributed tracing and metrics with W3C context propagation
- **Pluggable backends** — Memory, SQLite, PostgreSQL, Redis
- **Optimistic concurrency control** — version-tracked storage updates
- **SSE replay** — `Last-Event-ID` based reconnection with gap-fill
- **Debug UI** — built-in browser interface for chat + task inspection
- **Type-safe** — full type hints, `py.typed` marker, PEP 561 compliant
- **20+ examples** — echo, streaming, LangGraph, auth, middleware, push, DI, multi-transport, and more

## Links

- [Full Documentation](https://coding-crashkurse.github.io/a2akit/)
- [PyPI](https://pypi.org/project/a2akit/)
- [Changelog](https://github.com/Coding-Crashkurse/a2akit/blob/main/CHANGELOG.md)
- [Examples](https://github.com/Coding-Crashkurse/a2akit/tree/main/examples)

## License

MIT
