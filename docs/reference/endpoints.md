# Endpoints

a2akit serves all A2A protocol endpoints plus agent discovery and health checks.

## Endpoint Overview

| Method | Path | Description |
|--------|------|-------------|
| POST | `/v1/message:send` | Submit a message, return task or direct reply |
| POST | `/v1/message:stream` | Submit a message, stream events via SSE |
| GET | `/v1/tasks/{task_id}` | Get a single task by ID |
| GET | `/v1/tasks` | List tasks with filters and pagination |
| POST | `/v1/tasks/{task_id}:cancel` | Cancel a task |
| POST | `/v1/tasks/{task_id}:subscribe` | Subscribe to task updates via SSE |
| GET | `/v1/card` | Authenticated extended agent card |
| GET | `/v1/health` | Health check |
| GET | `/.well-known/agent-card.json` | Agent discovery card |

## POST `/v1/message:send`

Submit a message and return the task (or a direct message for `reply_directly()`).

**Request:**

```json
{
  "message": {
    "role": "user",
    "parts": [{"text": "Hello, agent!"}],
    "messageId": "msg-001"
  },
  "configuration": {
    "blocking": true
  }
}
```

- `messageId` is **required** and must be non-empty.
- When `configuration.blocking` is `true` (default), the server waits until the task completes or times out.
- Set `message.taskId` for follow-up messages on existing tasks.

**Response (task):**

```json
{
  "id": "task-uuid",
  "contextId": "ctx-uuid",
  "status": {"state": "completed", "timestamp": "..."},
  "artifacts": [...],
  "history": [...]
}
```

**Response (direct reply):**

```json
{
  "role": "agent",
  "parts": [{"text": "Quick answer"}],
  "messageId": "..."
}
```

## POST `/v1/message:stream`

!!! note "Requires `streaming` capability"
    This endpoint returns `400` with error code `-32004` if the agent has not enabled `CapabilitiesConfig(streaming=True)`.

Submit a message and receive SSE events. The stream contains:

1. Initial `Task` snapshot
2. `TaskStatusUpdateEvent` for state transitions
3. `TaskArtifactUpdateEvent` for artifact updates
4. Final `TaskStatusUpdateEvent` with `final: true`

**Request:** Same as `message:send`.

**Response:** `text/event-stream` with JSON payloads. Each event has a `kind` field as discriminator.

## GET `/v1/tasks/{task_id}`

Get a single task by ID.

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `historyLength` | `int` | Limit number of history messages returned |

**Response:** Task JSON or 404.

## GET `/v1/tasks`

List tasks with optional filters and pagination.

**Query Parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `contextId` | `string` | — | Filter by context ID |
| `status` | `string` | — | Filter by task state |
| `pageSize` | `int` | `50` | Results per page (1-100) |
| `pageToken` | `string` | — | Pagination token |
| `historyLength` | `int` | — | Limit history messages |
| `statusTimestampAfter` | `string` | — | Filter by timestamp |
| `includeArtifacts` | `bool` | `false` | Include artifacts in response |

**Response:**

```json
{
  "tasks": [...],
  "nextPageToken": "...",
  "pageSize": 50,
  "totalSize": 123
}
```

## POST `/v1/tasks/{task_id}:cancel`

Cancel a task. Signals the CancelRegistry and starts a force-cancel timer.

**Response:** Current task state or error.

| Status | Description |
|--------|-------------|
| 200 | Cancel signal sent, current task returned |
| 404 | Task not found |
| 409 | Task already in terminal state |

## POST `/v1/tasks/{task_id}:subscribe`

!!! note "Requires `streaming` capability"
    This endpoint returns `400` with error code `-32004` if the agent has not enabled `CapabilitiesConfig(streaming=True)`.

Subscribe to updates for an existing task via SSE. Yields the current task state first, then streams live events.

Supports `Last-Event-ID` header for reconnection (backends with replay support).

| Status | Description |
|--------|-------------|
| 200 | SSE stream started |
| 404 | Task not found |
| 400 | Task in terminal state (cannot subscribe) |

## GET `/v1/health`

Simple health check endpoint.

**Response:**

```json
{"status": "ok"}
```

## Push Notification Config Endpoints

!!! note "Requires `push_notifications` capability"
    These endpoints return `501` with error code `-32003` if the agent has not enabled `CapabilitiesConfig(push_notifications=True)`.

### POST `/v1/tasks/{task_id}/pushNotificationConfigs`

Create or update a push notification config for a task.

**Request:**

```json
{
  "url": "https://my-app.com/webhook",
  "token": "my-secret",
  "id": "optional-config-id",
  "authentication": {
    "schemes": ["Bearer"],
    "credentials": "jwt-token"
  }
}
```

**Response:** `TaskPushNotificationConfig`

```json
{
  "taskId": "task-uuid",
  "pushNotificationConfig": {
    "id": "config-uuid",
    "url": "https://my-app.com/webhook",
    "token": "my-secret"
  }
}
```

### GET `/v1/tasks/{task_id}/pushNotificationConfigs/{config_id}`

Get a specific push notification config.

**Response:** `TaskPushNotificationConfig` or 404.

### GET `/v1/tasks/{task_id}/pushNotificationConfigs`

List all push notification configs for a task.

**Response:** `TaskPushNotificationConfig[]`

### DELETE `/v1/tasks/{task_id}/pushNotificationConfigs/{config_id}`

Delete a push notification config.

**Response:** `204 No Content` or 404.

## GET `/v1/card`

!!! note "Requires `extended_card_provider`"
    Returns `404` with error code `-32007` if the agent has not configured an `extended_card_provider` on `A2AServer`.

Returns the authenticated extended agent card. The provider callback receives the full `Request` object, so it can inspect `Authorization` headers or other credentials to tailor the returned card (e.g., expose premium skills to authenticated callers).

**Response:** Full `AgentCard` JSON (same schema as `/.well-known/agent-card.json`).

| Status | Description |
|--------|-------------|
| 200 | Extended card returned |
| 404 | Extended card provider not configured (error code `-32007`) |

## GET `/.well-known/agent-card.json`

Agent discovery card with capabilities, skills, and transport information. The URL is derived from the request (proxy-aware via `X-Forwarded-Proto` and `X-Forwarded-Host` headers).

## A2A-Version Header

All endpoints validate the `A2A-Version` request header:

- **Missing**: Tolerated (spec assumes 0.3)
- **Matching**: Request proceeds
- **Mismatched**: Returns 400 with error code `-32009`

Pre-1.0: Major.Minor must match exactly.
Post-1.0: Major must match (semver compatibility).

```bash
curl -H "A2A-Version: 0.3.0" http://localhost:8000/v1/message:send ...
```

## Error Responses

| HTTP Status | Error Code | Description |
|-------------|-----------|-------------|
| 400 | -32600 | Invalid request (missing messageId) |
| 400 | -32602 | Context mismatch |
| 400 | -32004 | Streaming not supported (capability not enabled) |
| 400 | -32009 | Unsupported A2A version |
| 404 | -32001 | Task not found |
| 409 | -32002 | Task not cancelable |
| 409 | -32004 | Task in terminal state |
| 422 | -32602 | Task not accepting messages |
| 404 | -32007 | Authenticated extended card not configured |
| 501 | -32003 | Push notifications not supported |
| 503 | — | TaskManager not initialized |
