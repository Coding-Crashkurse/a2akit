# a2akit

[![PyPI](https://img.shields.io/pypi/v/a2akit)](https://pypi.org/project/a2akit/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/a2akit)](https://pypi.org/project/a2akit/)
[![CI](https://github.com/markuslang1987/a2akit/actions/workflows/ci.yml/badge.svg)](https://github.com/markuslang1987/a2akit/actions)

**A2A agent framework in one import.**

Build [Agent-to-Agent (A2A)](https://google.github.io/A2A/) protocol
agents with minimal boilerplate. Streaming, cancellation, multi-turn
conversations, and artifact handling — batteries included.

## Install

```bash
pip install a2akit
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
