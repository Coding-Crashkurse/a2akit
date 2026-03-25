# Redis + LangGraph Example

A full-stack production example with Redis broker/event bus, PostgreSQL storage, and a LangGraph research pipeline agent.

## Quick Start

```bash
cd examples/redis_langgraph
docker compose up --build
```

Then open [http://localhost:8000/chat](http://localhost:8000/chat) in your browser.

## What It Demonstrates

1. **Redis as broker, event bus, AND cancel registry** — horizontally scalable task processing
2. **PostgreSQL as durable storage** — tasks survive server restarts
3. **LangGraph StateGraph** with 3 nodes (research → analyze → summarize) and custom stream events
4. **Cooperative cancellation** inside graph nodes via `ctx.is_cancelled`
5. **Streaming artifact chunks** from LangGraph → A2A SSE
6. **Built-in debug UI** at `/chat`

## Architecture

```
┌──────────────┐     ┌──────────────┐     ┌──────────────┐
│  Agent (N)   │────▶│    Redis     │◀────│  Agent (N)   │
│  uvicorn     │     │  Streams +   │     │  uvicorn     │
│              │     │  Pub/Sub     │     │              │
└──────┬───────┘     └──────────────┘     └──────┬───────┘
       │                                         │
       └────────────┬────────────────────────────┘
                    ▼
           ┌──────────────┐
           │  PostgreSQL  │
           │  (storage)   │
           └──────────────┘
```

## Server Code

```python
from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig

server = A2AServer(
    worker=ResearchWorker(),
    agent_card=AgentCardConfig(
        name="Research Pipeline Agent",
        description="Multi-stage research agent powered by LangGraph.",
        version="1.0.0",
        capabilities=CapabilitiesConfig(streaming=True),
    ),
    broker="redis://redis:6379/0",          # RedisBroker
    event_bus="redis://redis:6379/0",       # RedisEventBus
    # cancel_registry auto-created as RedisCancelRegistry
    storage="postgresql+asyncpg://...",     # PostgreSQLStorage
)
```

## Scaling

```bash
# Run 3 worker replicas sharing the same Redis broker
docker compose up --build --scale agent=3
```

Each replica joins the same Redis consumer group — tasks are load-balanced automatically.

## CLI Client

```bash
python client.py "Research the current state of AI agent frameworks"
```

## Files

| File | Description |
|------|-------------|
| `server.py` | A2AServer with LangGraph worker |
| `client.py` | CLI client for terminal usage |
| `Dockerfile` | Python image with dependencies |
| `docker-compose.yml` | Redis + Postgres + Agent |
| `requirements.txt` | pip dependencies |
