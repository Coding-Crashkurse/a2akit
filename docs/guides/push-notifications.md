# Push Notifications

Push notifications allow clients to receive asynchronous task updates via HTTP webhooks instead of holding open SSE connections. This is critical for long-running tasks, mobile clients, and serverless architectures where persistent connections are impractical.

## Overview

When push notifications are enabled, the server POSTs task state updates to client-provided webhook URLs on **every state transition** — not just terminal states. Clients receive webhooks for:

- `submitted` -> `working` (task started)
- `working` -> `working` (status updates)
- `working` -> `input-required` (agent needs input)
- `working` -> `auth-required` (agent needs credentials)
- `working` -> `completed` / `failed` / `canceled` / `rejected` (terminal)

### SSE Streaming vs Push Notifications vs Polling

| Approach | Best for | Connection |
|----------|----------|------------|
| **SSE Streaming** | Real-time UIs, short tasks | Persistent HTTP connection |
| **Push Notifications** | Long-running tasks, mobile, serverless | No persistent connection needed |
| **Polling** | Simple clients, fallback | Periodic GET requests |

## Quick Start

### 1. Enable Push Notifications

```python
from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig

server = A2AServer(
    worker=MyWorker(),
    agent_card=AgentCardConfig(
        name="My Agent",
        description="Agent with push notification support",
        version="0.1.0",
        capabilities=CapabilitiesConfig(
            push_notifications=True,
        ),
    ),
    push_allow_http=True,  # Only for local development!
)
app = server.as_fastapi_app()
```

### 2. Set Up a Webhook Receiver

Any HTTP endpoint that accepts POST requests can be a webhook receiver:

```python
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

app = FastAPI()

@app.post("/webhook")
async def receive(request: Request, x_a2a_notification_token: str | None = Header(None)):
    if x_a2a_notification_token != "my-secret":
        return JSONResponse(status_code=401, content={"error": "Invalid token"})

    body = await request.json()
    task_id = body["id"]
    state = body["status"]["state"]
    print(f"Task {task_id}: {state}")

    return JSONResponse(content={"status": "received"})
```

### 3. Register a Push Config

```bash
# After creating a task, register a webhook:
curl -X POST http://localhost:8000/v1/tasks/{task_id}/pushNotificationConfigs \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://my-app.com/webhook",
    "token": "my-secret"
  }'
```

Or use the client:

```python
async with A2AClient("http://localhost:8000") as client:
    result = await client.send("Generate report", blocking=False)
    await client.set_push_config(
        result.task_id,
        url="https://my-app.com/webhook",
        token="my-secret",
    )
```

### 4. Inline Convenience

Set push config directly when sending a message:

```python
result = await client.send(
    "Generate the Q1 report",
    blocking=False,
    push_url="https://my-app.com/webhook",
    push_token="my-secret",
)
```

## Configuration

### Server-Side Options

Pass these to `A2AServer()`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `push_max_retries` | `3` | Max delivery attempts per webhook |
| `push_retry_delay` | `1.0` | Base delay between retries (exponential backoff) |
| `push_timeout` | `10.0` | HTTP timeout for webhook delivery |
| `push_max_concurrent` | `50` | Max concurrent webhook deliveries |
| `push_allow_http` | `False` | Allow HTTP (non-HTTPS) URLs (dev only!) |
| `push_idle_timeout` | `300.0` | Idle timeout (seconds) for delivery queue workers |
| `push_allowed_hosts` | `None` | Allowlist of webhook hostnames |
| `push_blocked_hosts` | `None` | Blocklist of webhook hostnames |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `A2AKIT_PUSH_MAX_RETRIES` | `3` | Max delivery attempts |
| `A2AKIT_PUSH_RETRY_DELAY` | `1.0` | Base retry delay (seconds) |
| `A2AKIT_PUSH_TIMEOUT` | `10.0` | Webhook HTTP timeout (seconds) |
| `A2AKIT_PUSH_MAX_CONCURRENT` | `50` | Max concurrent deliveries |
| `A2AKIT_PUSH_ALLOW_HTTP` | `False` | Allow HTTP URLs |
| `A2AKIT_PUSH_IDLE_TIMEOUT` | `300.0` | Idle timeout for delivery queues (seconds) |

## Webhook Security

### Token Validation

When a client provides a `token` in the push config, the server includes it as the `X-A2A-Notification-Token` header on every webhook delivery. Your receiver should validate this:

```python
if x_a2a_notification_token != expected_token:
    return JSONResponse(status_code=401, content={"error": "Invalid token"})
```

### Authentication Headers

Clients can provide authentication details:

```python
await client.set_push_config(
    task_id,
    url="https://my-app.com/webhook",
    authentication={"schemes": ["Bearer"], "credentials": "my-jwt-token"},
)
```

The server includes the `Authorization` header: `Authorization: Bearer my-jwt-token`.

### TLS Requirements

- **Production**: Only HTTPS URLs are accepted (default)
- **Development**: Set `push_allow_http=True` to allow HTTP

### SSRF Prevention

Webhook URLs are validated before delivery:

- Private IP ranges are blocked (10.x, 172.16.x, 192.168.x, 127.x, 169.254.x)
- IPv6 loopback and link-local addresses are blocked
- No redirect following (httpx configured with `follow_redirects=False`)
- Optional hostname allowlist/blocklist

## API Reference

### REST Endpoints (HTTP+JSON)

#### Set Config
```
POST /v1/tasks/{task_id}/pushNotificationConfigs
Body: { "url": "...", "token": "...", "id": "...", "authentication": {...} }
Response: TaskPushNotificationConfig
```

#### Get Config
```
GET /v1/tasks/{task_id}/pushNotificationConfigs/{config_id}
Response: TaskPushNotificationConfig
```

#### List Configs
```
GET /v1/tasks/{task_id}/pushNotificationConfigs
Response: TaskPushNotificationConfig[]
```

#### Delete Config
```
DELETE /v1/tasks/{task_id}/pushNotificationConfigs/{config_id}
Response: 204 No Content
```

### JSON-RPC Methods

| Method | Params | Result |
|--------|--------|--------|
| `tasks/pushNotificationConfig/set` | `{ taskId, pushNotificationConfig }` | `TaskPushNotificationConfig` |
| `tasks/pushNotificationConfig/get` | `{ id (taskId), pushNotificationConfigId? }` | `TaskPushNotificationConfig` |
| `tasks/pushNotificationConfig/list` | `{ id (taskId) }` | `TaskPushNotificationConfig[]` |
| `tasks/pushNotificationConfig/delete` | `{ id (taskId), pushNotificationConfigId }` | `null` |

### Capability Gate

All push endpoints require `capabilities.pushNotifications == True`. When disabled:

- REST: returns `501` with code `-32003`
- JSON-RPC: returns error code `-32003`

## Delivery Semantics

- **Best-effort** with configurable retries (exponential backoff)
- **All state transitions** trigger delivery (not just terminal)
- **Sequential per config** — events arrive in order for each webhook
- **Parallel across configs** — different webhooks receive events concurrently
- **No dead-letter queue** — failed deliveries are logged, not persisted
- **Non-blocking** — delivery failures never affect task processing

!!! tip "Idempotency"
    Webhook receivers should be idempotent. Due to retries, the same event may be delivered more than once. Use the task `id` and `status.timestamp` to deduplicate.

## Client Methods

```python
# Set a push config
await client.set_push_config(task_id, url="...", token="...", config_id="...")

# Get a push config
config = await client.get_push_config(task_id, config_id="...")

# List all push configs
configs = await client.list_push_configs(task_id)

# Delete a push config
await client.delete_push_config(task_id, config_id="...")

# Inline with send()
result = await client.send("...", push_url="...", push_token="...")
```

## Troubleshooting

### Webhook not receiving events

1. Check that `push_notifications=True` is set in `CapabilitiesConfig`
2. Verify the webhook URL is reachable from the server
3. For local dev, ensure `push_allow_http=True` is set
4. Check server logs for delivery errors

### Private IP rejected

SSRF prevention blocks private IPs by default. For local development, use `push_allow_http=True` and a public hostname or `ngrok`.

### Events arriving out of order

Events are guaranteed sequential **per config**. If you have multiple configs, they are delivered in parallel and may interleave.
