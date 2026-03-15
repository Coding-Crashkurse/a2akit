# Debug UI

a2akit ships with a built-in browser-based debug interface for testing and inspecting your A2A agents during development. Think of it as FastAPI's `/docs`, but for A2A agents.

![Debug UI — Task Dashboard](../images/img1.png)

## Activation

Pass `debug=True` when creating the FastAPI app:

```python
from a2akit import A2AServer, AgentCardConfig, TaskContext, Worker


class EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


server = A2AServer(
    worker=EchoWorker(),
    agent_card=AgentCardConfig(
        name="Echo",
        description="Echoes your input back.",
        version="0.1.0",
        protocol="http+json",
    ),
)
app = server.as_fastapi_app(debug=True)
```

```bash
uvicorn my_agent:app --reload
```

Then open **`http://localhost:8000/chat`** in your browser.

When `debug=False` (the default), the `/chat` route is not mounted and there is zero overhead.

## Chat View

The Chat view lets you send messages to your agent and see responses in real time.

- Each message creates a **new task** with a fresh `messageId`.
- When the agent responds with `input-required` or `auth-required`, the UI automatically continues the same task on the next message.
- Once a terminal state is reached (`completed`, `failed`, `canceled`, `rejected`), the next message starts a fresh task.
- **Streaming agents** show artifact chunks live as they arrive via SSE.
- **Non-streaming agents** send with `configuration.blocking: true` and show a loading indicator.
- Every agent response displays a **state badge** (`completed`, `failed`, etc.).
- Click **New Conversation** to clear chat history and stored task/context IDs.

## Task Dashboard

The Tasks view shows all tasks in the system with live polling.

- **Task list** — sorted by timestamp (newest first), each row shows Task ID, state badge, context ID, and timestamp.
- **Configurable polling** — use the dropdown in the top-right to set the polling interval (0.5s, 1s, 2s, 5s, 10s, or 30s). You can also set the initial interval via query parameter: `/chat?poll=5` for 5-second polling.
- **Task detail** — click any row to expand and see:
    - Full Task ID and Context ID
    - Current state with timestamp
    - Status message
    - **State transition timeline** — vertical timeline showing every state change with badge, timestamp, and optional message text (requires `state_transition_history` capability or always present in metadata)
    - Complete message history (user + agent messages)
    - All artifacts with text and formatted JSON
    - Metadata (excluding internal `_`-prefixed keys and `stateTransitions`)
- Polling updates task states in-place without flickering (React virtual DOM).

## Protocol Support

The Debug UI works with both A2A transports transparently:

| | HTTP+JSON | JSON-RPC |
|---|---|---|
| **Chat — send** | `POST /v1/message:send` | `POST /` method `message/send` |
| **Chat — stream** | `POST /v1/message:stream` | `POST /` method `message/sendStream` |
| **Tasks — list** | `GET /v1/tasks` | `POST /` method `tasks/list` |
| **Tasks — detail** | `GET /v1/tasks/{id}` | `POST /` method `tasks/get` |

The protocol is auto-detected from the agent card's `preferredTransport` field.

## Agent Info Sidebar

The left sidebar (visible in both views) displays agent metadata read from `/.well-known/agent-card.json`:

- Name, description, version
- Protocol (JSON-RPC or HTTP+JSON)
- A2A protocol version
- Agent URL
- Streaming capability (checkmark or cross)
- State History capability (checkmark or cross)
- Input/output modes
- Registered skills

## Technical Details

- The UI is a **single self-contained HTML file** (~220 KB) with all CSS and JS inlined.
- Built with **React + Vite**, bundled via `vite-plugin-singlefile`. No external JS dependencies at runtime.
- The HTML is stored as a Python string constant in `_chat_ui.py` and served via `HTMLResponse`.
- The route is registered with `include_in_schema=False` so it does not appear in the OpenAPI spec.
- The package version is injected at serve time via `CHAT_HTML.replace("{{VERSION}}", ...)`.

### Rebuilding the UI

If you modify the UI source code in the `ui/` directory:

```bash
cd ui
npm install
npm run build    # Vite build → dist/index.html
npm run embed    # Writes dist/index.html into src/a2akit/_chat_ui.py
```

## Scope

The Debug UI is a **development tool**, not a production feature:

- No persistent state (refreshing the page resets the chat; the dashboard re-fetches from the server)
- No file upload, no auth flows, no cancel button
- No markdown rendering (plain text only, JSON displayed in `<pre>` blocks)
- No mobile-optimized layout
- No WebSocket (SSE + polling only)
