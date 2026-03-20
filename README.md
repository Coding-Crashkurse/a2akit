# a2akit

[![PyPI](https://img.shields.io/pypi/v/a2akit)](https://pypi.org/project/a2akit/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/a2akit)](https://pypi.org/project/a2akit/)
[![CI](https://github.com/Coding-Crashkurse/a2a-kit/actions/workflows/ci.yml/badge.svg)](https://github.com/Coding-Crashkurse/a2a-kit/actions)

**A2A agent framework in one import.**

Build [Agent-to-Agent (A2A)](https://google.github.io/A2A/) protocol agents with minimal boilerplate.
Streaming, cancellation, multi-turn conversations, and artifact handling — batteries included.

## Install

```bash
pip install a2akit
```

With optional extras:

```bash
pip install a2akit[langgraph]   # LangGraph integration
pip install a2akit[otel]        # OpenTelemetry tracing & metrics
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

```bash
uvicorn my_agent:app --reload
```

## Client

```python
from a2akit import A2AClient

async with A2AClient("http://localhost:8000") as client:
    result = await client.send("Hello, agent!")
    print(result.text)

    async for chunk in client.stream_text("Stream me"):
        print(chunk, end="")
```

## Debug UI

Enable the built-in debug interface to test your agent in the browser:

```python
app = server.as_fastapi_app(debug=True)
```

Open `http://localhost:8000/chat` — chat with your agent and inspect tasks in real time.

![Debug UI](docs/images/img1.png)

## Features

- **Debug UI** — built-in browser interface for chat + task inspection (`debug=True`)
- **One-liner setup** — `A2AServer` wires storage, broker, event bus, and endpoints
- **A2AClient** — auto-discovers agents, supports send/stream/cancel/subscribe
- **Capabilities** — explicit opt-in for streaming, state transition history, and extensions, enforced on server and client
- **Extensions** — declare A2A protocol extensions on your agent card (`ExtensionConfig`)
- **Streaming** — word-by-word artifact streaming via SSE
- **Cancellation** — cooperative and force-cancel with timeout fallback
- **Multi-turn** — `request_input()` / `request_auth()` for conversational flows
- **Direct reply** — `reply_directly()` for simple request/response without task tracking
- **Middleware** — pipeline for auth extraction, header injection, payload sanitization
- **Push notifications** — webhook delivery for async task updates with SSRF prevention and configurable retries
- **Lifecycle hooks** — fire-and-forget callbacks on terminal state transitions
- **Dependency injection** — shared infrastructure with automatic lifecycle management
- **OpenTelemetry** — opt-in distributed tracing and metrics (`pip install a2akit[otel]`)
- **Pluggable backends** — PostgreSQL, SQLite, and more (Redis, RabbitMQ coming soon)
- **Type-safe** — full type hints, `py.typed` marker, PEP 561 compliant

📖 **[Full Documentation](https://coding-crashkurse.github.io/a2a-kit/)**

## Links

- [PyPI](https://pypi.org/project/a2a-kit/)
- [GitHub](https://github.com/Coding-Crashkurse/a2a-kit)
- [Changelog](https://github.com/Coding-Crashkurse/a2a-kit/blob/main/CHANGELOG.md)

## License

MIT
