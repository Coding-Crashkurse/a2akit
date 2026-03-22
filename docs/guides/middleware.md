# Middleware

Middleware intercepts requests at the HTTP boundary, letting you extract secrets, inject headers, sanitize payloads, and log request/response metrics — all before your worker sees the data.

## Example

```python
from a2akit import (
    A2AMiddleware,
    A2AServer,
    AgentCardConfig,
    RequestEnvelope,
    TaskContext,
    Worker,
)
from fastapi import Request


class SecretExtractor(A2AMiddleware):
    """Move sensitive keys from message.metadata into transient context."""

    SECRET_KEYS = {"user_token", "api_key", "auth_token"}

    async def before_dispatch(
        self, envelope: RequestEnvelope, request: Request
    ) -> None:
        msg_meta = envelope.params.message.metadata or {}
        for key in self.SECRET_KEYS & msg_meta.keys():
            envelope.context[key] = msg_meta.pop(key)  # (1)!

        if auth := request.headers.get("Authorization"):
            envelope.context["auth_header"] = auth  # (2)!


class MyWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        token = ctx.request_context.get("user_token")  # (3)!
        trace_id = ctx.metadata.get("trace_id")  # (4)!
        await ctx.complete(f"Token present: {token is not None}")


server = A2AServer(
    worker=MyWorker(),
    agent_card=AgentCardConfig(
        name="My Agent", description="...", version="0.1.0"
    ),
    middlewares=[SecretExtractor()],  # (5)!
)
app = server.as_fastapi_app()
```

1. Secrets are **removed** from `message.metadata` (which gets persisted) and **moved** to `envelope.context` (transient).
2. HTTP headers can also be captured into the transient context.
3. In the worker, access transient data via `ctx.request_context`. This data is **never** written to Storage.
4. Non-secret metadata stays in `ctx.metadata` and **is** persisted.
5. Register middlewares as a list. They run in order.

## RequestEnvelope

The `RequestEnvelope` is the unit of work that flows through the middleware pipeline:

```python
@dataclass
class RequestEnvelope:
    params: MessageSendParams  # A2A protocol payload (persisted)
    context: dict[str, Any]    # transient metadata (never persisted)
```

- **`params`** — The A2A protocol payload. Storage only ever sees this.
- **`context`** — Framework-internal metadata. Populated by middleware, consumed by the Worker via `ctx.request_context`. Discarded after the Worker finishes.

## Middleware Methods

### `before_dispatch(envelope, request)`

Called before TaskManager processes the request. Mutate the envelope in-place:

- Extract secrets from `envelope.params.message.metadata` and move to `envelope.context`
- Read HTTP headers from `request` into `envelope.context`
- Sanitize or validate `envelope.params`

### `after_dispatch(envelope, result)`

Called after TaskManager returns, before the HTTP response is sent. The same `envelope` object is available:

- Log timing and metrics
- Emit audit events
- Clean up transient resources

## Execution Order

!!! note "Middleware execution order"
    `before_dispatch` runs in **registration order**. `after_dispatch` runs in **reverse order** — like Python context managers or a stack.

    ```python
    middlewares=[AuthMiddleware(), LoggingMiddleware()]
    ```

    Execution:

    1. `AuthMiddleware.before_dispatch`
    2. `LoggingMiddleware.before_dispatch`
    3. *TaskManager processes request*
    4. `LoggingMiddleware.after_dispatch`
    5. `AuthMiddleware.after_dispatch`

## Secrets vs. Metadata

| | `ctx.metadata` | `ctx.request_context` |
|---|---|---|
| Source | `message.metadata` | `envelope.context` |
| Persisted in Storage? | Yes | **No** |
| Use for | Trace IDs, correlation, non-sensitive data | Tokens, API keys, auth headers |
| Available in | Worker, Storage, EventBus | Worker only |

!!! warning "Never persist secrets"
    Always move sensitive data (tokens, API keys, passwords) from `metadata` to `context` in your middleware. Data left in `metadata` is written to Storage and may appear in task history.

## Built-in Auth Middlewares

a2akit ships two ready-made auth middlewares. Both raise `AuthenticationRequiredError` which the server maps to HTTP 401 with a `WWW-Authenticate` header.

### BearerTokenMiddleware

Validates `Authorization: Bearer <token>` headers via a user-provided async verify function:

```python
from a2akit import A2AServer, AgentCardConfig, BearerTokenMiddleware

async def verify(token: str) -> dict | None:
    """Return claims dict or None if invalid."""
    if token == "valid-token":
        return {"sub": "user1"}
    return None

server = A2AServer(
    worker=...,
    agent_card=AgentCardConfig(name="Protected Agent", description="..."),
    middlewares=[BearerTokenMiddleware(verify=verify)],
)
```

On success, the claims dict is available via `ctx.request_context["auth_claims"]` and the raw token via `ctx.request_context["auth_token"]`.

### ApiKeyMiddleware

Validates API keys from a configurable header (default: `X-API-Key`):

```python
from a2akit import A2AServer, AgentCardConfig, ApiKeyMiddleware

server = A2AServer(
    worker=...,
    agent_card=AgentCardConfig(name="API Key Agent", description="..."),
    middlewares=[ApiKeyMiddleware(valid_keys={"sk-abc123", "sk-def456"})],
)
```

The validated key is available via `ctx.request_context["api_key"]`.

### Path Exclusion

Both middlewares accept `exclude_paths` to skip authentication for specific routes (default: `{"/v1/health"}`). Agent Card Discovery (`GET /.well-known/agent-card.json`) runs outside the middleware pipeline and never requires authentication.

### AuthenticationRequiredError

You can also raise `AuthenticationRequiredError` from your own custom middleware:

```python
from a2akit import A2AMiddleware, RequestEnvelope
from a2akit.errors import AuthenticationRequiredError
from fastapi import Request

class CustomAuth(A2AMiddleware):
    async def before_dispatch(self, envelope: RequestEnvelope, request: Request) -> None:
        if not request.headers.get("X-Custom-Auth"):
            raise AuthenticationRequiredError(scheme="Custom", realm="myapp")
```

The server returns HTTP 401 with `WWW-Authenticate: Custom realm="myapp"`.
