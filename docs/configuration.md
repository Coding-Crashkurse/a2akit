# Configuration

a2akit reads settings from environment variables prefixed with `A2AKIT_`. Every setting has a sensible default; explicit constructor parameters always take priority.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `A2AKIT_BLOCKING_TIMEOUT` | `30.0` | Seconds `message:send` blocks for a result |
| `A2AKIT_CANCEL_FORCE_TIMEOUT` | `60.0` | Seconds before force-cancel kicks in |
| `A2AKIT_MAX_CONCURRENT_TASKS` | *(none)* | Worker parallelism (`None` = unlimited) |
| `A2AKIT_MAX_RETRIES` | `3` | Broker retry attempts on worker crash |
| `A2AKIT_BROKER_BUFFER` | `1000` | InMemoryBroker queue depth |
| `A2AKIT_EVENT_BUFFER` | `200` | InMemoryEventBus fan-out buffer per task |
| `A2AKIT_LOG_LEVEL` | *(unset)* | Root `a2akit` logger level (e.g. `DEBUG`) |
| `A2AKIT_PUSH_MAX_RETRIES` | `3` | Max webhook delivery attempts |
| `A2AKIT_PUSH_RETRY_DELAY` | `1.0` | Base delay between retries (exponential backoff) |
| `A2AKIT_PUSH_TIMEOUT` | `10.0` | HTTP timeout for webhook delivery |
| `A2AKIT_PUSH_MAX_CONCURRENT` | `50` | Max concurrent webhook deliveries |
| `A2AKIT_PUSH_ALLOW_HTTP` | `False` | Allow HTTP webhook URLs (dev only) |

### Redis Settings

These settings apply when using the Redis broker, event bus, or cancel registry (`pip install a2akit[redis]`).

| Variable | Default | Description |
|----------|---------|-------------|
| `A2AKIT_REDIS_URL` | `redis://localhost:6379/0` | Default Redis connection URL |
| `A2AKIT_REDIS_KEY_PREFIX` | `a2akit:` | Key prefix for multi-tenant isolation |
| `A2AKIT_REDIS_BROKER_STREAM` | `tasks` | Stream name for the broker queue |
| `A2AKIT_REDIS_BROKER_GROUP` | `workers` | Consumer group name |
| `A2AKIT_REDIS_BROKER_CONSUMER_PREFIX` | `worker` | Consumer name prefix (hostname+PID appended) |
| `A2AKIT_REDIS_BROKER_BLOCK_MS` | `5000` | XREADGROUP block timeout (ms) |
| `A2AKIT_REDIS_BROKER_CLAIM_TIMEOUT_MS` | `60000` | XAUTOCLAIM idle threshold (ms) |
| `A2AKIT_REDIS_EVENT_BUS_CHANNEL_PREFIX` | `events:` | Pub/Sub channel prefix |
| `A2AKIT_REDIS_EVENT_BUS_STREAM_PREFIX` | `eventlog:` | Replay stream prefix |
| `A2AKIT_REDIS_EVENT_BUS_STREAM_MAXLEN` | `1000` | Max replay stream length per task |
| `A2AKIT_REDIS_CANCEL_KEY_PREFIX` | `cancel:` | Cancel flag key prefix |
| `A2AKIT_REDIS_CANCEL_TTL_S` | `86400` | Cancel key TTL in seconds (24h) |

## Priority

Settings are resolved in this order (highest priority first):

1. **Constructor parameter** — explicit values passed to `A2AServer()`
2. **Environment variable** — `A2AKIT_*` prefix
3. **Built-in default** — hardcoded in `Settings`

```
Constructor > Env-Var > Default
```

## Example

Set environment variables:

```bash
export A2AKIT_BLOCKING_TIMEOUT=10
export A2AKIT_LOG_LEVEL=DEBUG
export A2AKIT_MAX_CONCURRENT_TASKS=4
```

## Programmatic Override

Use `Settings()` to override defaults programmatically:

```python
from a2akit import A2AServer, Settings

custom = Settings(blocking_timeout=5.0, max_retries=5)
server = A2AServer(worker=..., agent_card=..., settings=custom)
```

## Settings Class

```python
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="A2AKIT_")

    blocking_timeout: float = 30.0
    cancel_force_timeout: float = 60.0
    max_concurrent_tasks: int | None = None
    max_retries: int = 3
    broker_buffer: int = 1000
    event_buffer: int = 200
    log_level: str | None = None

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = "a2akit:"
    redis_broker_stream: str = "tasks"
    redis_broker_group: str = "workers"
    redis_broker_consumer_prefix: str = "worker"
    redis_broker_block_ms: int = 5000
    redis_broker_claim_timeout_ms: int = 60000
    redis_event_bus_channel_prefix: str = "events:"
    redis_event_bus_stream_prefix: str = "eventlog:"
    redis_event_bus_stream_maxlen: int = 1000
    redis_cancel_key_prefix: str = "cancel:"
    redis_cancel_ttl_s: int = 86400  # 24h

    # Push notification settings
    push_max_retries: int = 3
    push_retry_delay: float = 1.0
    push_timeout: float = 10.0
    push_max_concurrent: int = 50
    push_allow_http: bool = False
```

The `Settings` class uses [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) for automatic environment variable binding.

## A2AServer Constructor Parameters

These constructor parameters override Settings values:

| Parameter | Settings Field | Description |
|-----------|---------------|-------------|
| `blocking_timeout_s` | `blocking_timeout` | Blocking timeout in seconds |
| `cancel_force_timeout_s` | `cancel_force_timeout` | Force-cancel timeout |
| `max_concurrent_tasks` | `max_concurrent_tasks` | Worker parallelism |
| `push_max_retries` | `push_max_retries` | Webhook delivery retries |
| `push_retry_delay` | `push_retry_delay` | Retry base delay |
| `push_timeout` | `push_timeout` | Webhook HTTP timeout |
| `push_max_concurrent` | `push_max_concurrent` | Concurrent delivery limit |
| `push_allow_http` | `push_allow_http` | Allow HTTP webhook URLs |
| `push_allowed_hosts` | — | Hostname allowlist (constructor only) |
| `push_blocked_hosts` | — | Hostname blocklist (constructor only) |

!!! tip "Development vs. Production"
    For development, the defaults work well. For production, consider:

    - Setting `A2AKIT_MAX_CONCURRENT_TASKS` to limit resource usage
    - Reducing `A2AKIT_BLOCKING_TIMEOUT` for faster client feedback
    - Setting `A2AKIT_LOG_LEVEL=INFO` for structured logging
