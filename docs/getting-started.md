# Getting Started

## Installation

=== "pip"

    ```bash
    pip install a2akit
    ```

=== "pip + LangGraph"

    ```bash
    pip install a2akit[langgraph]
    ```

=== "pip + Redis"

    ```bash
    pip install a2akit[redis]
    ```

=== "pip + PostgreSQL"

    ```bash
    pip install a2akit[postgres]
    ```

=== "pip + SQLite"

    ```bash
    pip install a2akit[sqlite]
    ```

## Your First Agent

Create a file called `my_agent.py`:

```python
from a2akit import A2AServer, AgentCardConfig, TaskContext, Worker


class EchoWorker(Worker):  # (1)!
    async def handle(self, ctx: TaskContext) -> None:  # (2)!
        await ctx.complete(f"Echo: {ctx.user_text}")  # (3)!


server = A2AServer(
    worker=EchoWorker(),
    agent_card=AgentCardConfig(  # (4)!
        name="Echo Agent",
        description="Echoes your input back.",
        version="0.1.0",
    ),
)
app = server.as_fastapi_app()  # (5)!
```

1. Subclass `Worker` to define your agent's behavior.
2. Implement the single required method: `handle(ctx)`.
3. Call a lifecycle method to complete the task with a text artifact.
4. Configure the agent discovery card (served at `/.well-known/agent-card.json`).
5. Get a fully configured FastAPI app — storage, broker, event bus, and all endpoints wired automatically.

## Start the Server

```bash
uvicorn my_agent:app --reload
```

Your agent is now running at `http://localhost:8000` with all A2A endpoints active.

## Test It

```bash
curl -X POST http://localhost:8000/v1/message:send \
  -H "Content-Type: application/json" \
  -d '{"message":{"role":"user","parts":[{"text":"hello"}],"messageId":"1"}}'
```

Expected response:

```json
{
  "id": "...",
  "contextId": "...",
  "status": {
    "state": "completed",
    "timestamp": "..."
  },
  "artifacts": [
    {
      "artifactId": "final-answer",
      "parts": [{"text": "Echo: hello"}]
    }
  ],
  "history": [...]
}
```

## Discover the Agent

```bash
curl http://localhost:8000/.well-known/agent-card.json | python -m json.tool
```

This returns the agent's discovery card with capabilities, skills, and supported transport protocols.

## What's Next?

- [**Streaming**](guides/streaming.md) — emit artifacts word by word via SSE
- [**Middleware**](guides/middleware.md) — extract secrets, inject headers, sanitize payloads
- [**Dependency Injection**](guides/dependency-injection.md) — share DB pools, HTTP clients, and config
- [**Architecture**](concepts/architecture.md) — understand how the components fit together
