[![PyPI](https://img.shields.io/pypi/v/a2akit)](https://pypi.org/project/a2akit/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/markuslang1987/a2akit/blob/main/LICENSE)
[![Python](https://img.shields.io/pypi/pyversions/a2akit)](https://pypi.org/project/a2akit/)
[![CI](https://github.com/markuslang1987/a2akit/actions/workflows/ci.yml/badge.svg)](https://github.com/markuslang1987/a2akit/actions)

# a2akit

**A2A agent framework in one import.**

Build [Agent-to-Agent (A2A)](https://google.github.io/A2A/) protocol agents with minimal boilerplate.
Streaming, cancellation, multi-turn conversations, and artifact handling — batteries included.

---

<div class="grid cards" markdown>

-   :material-rocket-launch:{ .lg .middle } **One-liner setup**

    ---

    `A2AServer` wires storage, broker, event bus, and endpoints automatically.

-   :material-transfer:{ .lg .middle } **Streaming**

    ---

    Word-by-word artifact streaming via SSE with `emit_text_artifact`.

-   :material-cancel:{ .lg .middle } **Cancellation**

    ---

    Cooperative and force-cancel with configurable timeout fallback.

-   :material-chat-processing:{ .lg .middle } **Multi-turn**

    ---

    `request_input()` / `request_auth()` for conversational flows.

-   :material-middleware:{ .lg .middle } **Middleware**

    ---

    Pipeline for auth extraction, header injection, and payload sanitization.

-   :material-hook:{ .lg .middle } **Lifecycle Hooks**

    ---

    Fire-and-forget callbacks on terminal state transitions.

-   :material-language-python:{ .lg .middle } **Type-safe**

    ---

    Full type hints, `py.typed` marker, PEP 561 compliant.

-   :material-swap-horizontal:{ .lg .middle } **Pluggable backends**

    ---

    PostgreSQL, SQLite, Redis broker/event bus/cancel registry — swap independently.

</div>

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

```bash
curl -X POST http://localhost:8000/v1/message:send \
  -H "Content-Type: application/json" \
  -d '{"message":{"role":"user","parts":[{"text":"hello"}],"messageId":"1"}}'
```

---

[Get Started](getting-started.md){ .md-button .md-button--primary }
[Architecture](concepts/architecture.md){ .md-button }
