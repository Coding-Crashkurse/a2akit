# Deploying to Production

This guide covers deploying an a2akit agent with Redis, PostgreSQL, and multiple Uvicorn workers behind a reverse proxy.

## Architecture

```
                    ┌──────────┐
    Clients ──────► │  nginx   │
                    └────┬─────┘
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
         ┌────────┐ ┌────────┐ ┌────────┐
         │ worker │ │ worker │ │ worker │  (Uvicorn)
         └───┬────┘ └───┬────┘ └───┬────┘
             │          │          │
        ┌────┴──────────┴──────────┴────┐
        │           Redis               │  Broker + EventBus + CancelRegistry
        └───────────────────────────────┘
        ┌───────────────────────────────┐
        │         PostgreSQL            │  Storage
        └───────────────────────────────┘
```

In development, a2akit uses in-memory backends for everything. In production, you need shared state across workers — Redis for coordination, PostgreSQL for durable storage.

## Docker Compose

```yaml
services:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 3

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: a2akit
      POSTGRES_PASSWORD: a2akit
      POSTGRES_DB: a2akit
    ports: ["5432:5432"]
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U a2akit"]
      interval: 5s
      timeout: 3s
      retries: 3

  agent:
    build: .
    ports: ["8000:8000"]
    environment:
      REDIS_URL: redis://redis:6379/0
      DATABASE_URL: postgresql+asyncpg://a2akit:a2akit@postgres:5432/a2akit
      UVICORN_WORKERS: "4"
    depends_on:
      redis: { condition: service_healthy }
      postgres: { condition: service_healthy }

volumes:
  pgdata:
```

## Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir .[redis,postgres]

COPY . .

CMD ["uvicorn", "my_agent:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4"]
```

## Agent Configuration

```python
from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig, Worker

server = A2AServer(
    worker=MyWorker(),
    agent_card=AgentCardConfig(
        name="My Agent",
        description="Production agent.",
        version="1.0.0",
        capabilities=CapabilitiesConfig(streaming=True),
    ),
    # Redis handles broker, event bus, and cancel registry
    broker="redis://redis:6379/0",
    event_bus="redis://redis:6379/0",
    # PostgreSQL for durable storage
    storage="postgresql+asyncpg://a2akit:a2akit@postgres:5432/a2akit",
)
app = server.as_fastapi_app()
```

Passing a Redis URL to `broker` and `event_bus` automatically creates `RedisBroker`, `RedisEventBus`, and `RedisCancelRegistry` with a shared connection pool.

## Scaling

Scale horizontally by adding more workers:

```bash
docker compose up --build --scale agent=3
```

All workers share the same Redis and PostgreSQL instances, so tasks are distributed automatically via Redis Streams consumer groups.

## Reverse Proxy (nginx)

```nginx
upstream a2akit {
    server agent:8000;
}

server {
    listen 80;

    location / {
        proxy_pass http://a2akit;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # SSE streaming — disable buffering
    location ~ ^/v1/message:stream|/v1/tasks/.+:subscribe {
        proxy_pass http://a2akit;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
    }
}
```

!!! warning "SSE Buffering"
    Reverse proxies buffer responses by default, which breaks SSE streaming. Always set `proxy_buffering off` for stream endpoints.

## Environment Variables

All `A2AServer` constructor arguments can be set via environment variables:

| Variable | Description | Example |
|----------|-------------|---------|
| `REDIS_URL` | Redis connection string | `redis://redis:6379/0` |
| `DATABASE_URL` | PostgreSQL connection string | `postgresql+asyncpg://...` |
| `A2AKIT_MAX_CONCURRENT_TASKS` | Max parallel task processing | `10` |
| `A2AKIT_DEFAULT_BLOCKING_TIMEOUT` | Blocking request timeout (seconds) | `30` |
| `A2AKIT_PUSH_ALLOW_HTTP` | Allow HTTP webhooks (dev only!) | `false` |

See the [Configuration](../configuration.md) page for the complete list.

## Production Checklist

- [ ] Use Redis, PostgreSQL, or SQLite storage — not `InMemoryStorage`
- [ ] Use Redis for broker, event bus, and cancel registry
- [ ] Run multiple Uvicorn workers (`--workers N`)
- [ ] Put a reverse proxy in front (nginx, Caddy, or cloud LB)
- [ ] Disable SSE buffering in your proxy
- [ ] Set `A2AKIT_PUSH_ALLOW_HTTP=false` (default)
- [ ] Set `debug=False` (default) — the debug UI is not for production
- [ ] Configure `max_concurrent_tasks` to match your resource budget
- [ ] Set up health checks on `/v1/health`
- [ ] Add rate limiting at the gateway level (nginx `limit_req`, API Gateway, etc.)
