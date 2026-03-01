# a2akit

A2A-compliant agent framework. One `Worker` class, one `handle()` method — no protocol plumbing in your business logic.

## Why a2akit?

The official A2A SDK gives you low-level building blocks: `EventQueue`, `TaskUpdater`, `RequestContext`, `Part(root=TextPart(...))`, `ServerError`, `new_task()`, ... all of this leaks into your agent code.

Here's a cancelable long-running task with the **raw A2A SDK**:

```python
class MyExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue):
        task = context.current_task or new_task(context.message)
        await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        await updater.update_status(
            TaskState.working,
            updater.new_agent_message([Part(root=TextPart(text="Working..."))]),
        )
        # ... do work, manually check cancel flags, build Part objects ...
        await updater.add_artifact(
            [Part(root=TextPart(text="Result"))], name="result.txt"
        )
        await updater.complete(
            updater.new_agent_message([Part(root=TextPart(text="Done"))])
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        task = context.current_task
        state = task.status.state if task.status else None
        if state in {TaskState.completed, TaskState.failed, ...}:
            raise ServerError(TaskNotCancelableError(message="..."))
        # ... manual cancel event management ...
```

You need to understand `EventQueue`, `TaskUpdater`, `RequestContext`, `Part`, `TextPart`, `ServerError`, `new_task`, `TaskNotCancelableError` — all protocol internals that have nothing to do with what your agent actually does.

The same thing with **a2akit**:

```python
class MyWorker(Worker):
    async def handle(self, ctx: TaskContext):
        await ctx.send_status("Working...")
        # ... do work, check ctx.is_cancelled ...
        await ctx.complete("Result")
```

That's it. Cancellation, state machines, persistence, streaming, error handling, artifact management — all handled by the framework. Your code only contains business logic.

## TaskContext API

`TaskContext` is the only interface your worker needs. It abstracts away all A2A protocol details:

| Method / Property | Description |
| --- | --- |
| `ctx.user_text` | The user's input as plain text |
| `ctx.parts` | Raw message parts (text, files, etc.) |
| `ctx.files` | File parts as `list[FileInfo]` (content, url, filename, media_type) |
| `ctx.data_parts` | Structured data parts as `list[dict]` |
| `ctx.task_id` | Current task UUID |
| `ctx.context_id` | Conversation / context identifier |
| `ctx.message_id` | ID of the triggering message |
| `ctx.metadata` | Arbitrary metadata from the request |
| `ctx.is_cancelled` | Check if cancellation was requested |
| `ctx.turn_ended` | Whether a terminal method was called |
| `ctx.history` | Previous messages in this task (`list[HistoryMessage]`) |
| `ctx.previous_artifacts` | Artifacts from prior turns (`list[PreviousArtifact]`) |
| **Lifecycle** | |
| `ctx.complete(text?)` | Mark task completed with optional text artifact |
| `ctx.complete_json(data)` | Complete with a JSON data artifact |
| `ctx.respond(text?)` | Complete with a direct message (no artifact) |
| `ctx.reply_directly(text)` | Return a Message directly without task tracking |
| `ctx.fail(reason)` | Mark task failed |
| `ctx.reject(reason?)` | Reject the task |
| `ctx.request_input(question)` | Ask user for more input |
| `ctx.request_auth(details?)` | Request secondary authentication |
| **Streaming** | |
| `ctx.send_status(msg)` | Emit intermediate status update |
| `ctx.emit_text_artifact(...)` | Emit a text artifact chunk |
| `ctx.emit_data_artifact(data)` | Emit a structured data artifact chunk |
| `ctx.emit_artifact(...)` | Emit an artifact with any content (text, data, file_bytes, file_url) |
| **Context** | |
| `ctx.load_context()` | Load stored context for this conversation |
| `ctx.update_context(data)` | Store context for this conversation |

No `Part(root=TextPart(...))`. No `EventQueue`. No `TaskUpdater`. Just call methods.

## Setup

```bash
uv sync
```

For the LangGraph example:

```bash
uv sync --extra langgraph
```

## Examples

Three examples in the project root, each a standalone A2A agent:

| File | Pattern | Description |
| --- | --- | --- |
| `example_1.py` | Simple response | Returns text, framework handles artifacts + completion |
| `example_2.py` | Streaming artifacts | Emits word-by-word chunks via `ctx.emit_text_artifact()` |
| `example_3.py` | LangGraph pipeline | Runs a LangGraph graph with custom streaming, no LLM |

### Run

```bash
uv run uvicorn example_1:app --reload
uv run uvicorn example_2:app --reload
uv run uvicorn example_3:app --reload
```

### Test

```bash
curl http://localhost:8000/v1/health
curl http://localhost:8000/.well-known/agent-card.json

curl -X POST http://localhost:8000/v1/message:send \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "role": "user",
      "messageId": "00000000-0000-0000-0000-000000000001",
      "parts": [{"kind": "text", "text": "Hello!"}]
    }
  }'

# Stream (SSE)
curl -N -X POST http://localhost:8000/v1/message:stream \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "role": "user",
      "messageId": "00000000-0000-0000-0000-000000000002",
      "parts": [{"kind": "text", "text": "Hello!"}]
    }
  }'

# Get task by ID (replace with a real task ID from a previous response)
curl http://localhost:8000/v1/tasks/TASK_ID_HERE
```

## Architecture

`a2akit` allows you to bring your own `Storage`, `Broker`, `EventBus`, `CancelRegistry` and `Worker`.
You can also leverage the in-memory implementations for development.

Let's have a look at how those components fit together:

```mermaid
graph TD
    HTTP["HTTP Server\n(FastAPI + SSE)"]
    TM["TaskManager\n(coordinates)"]
    WA["WorkerAdapter\n(lifecycle)"]
    EE["EventEmitter\n(façade)"]
    B["Broker\n(queues & schedules)"]
    S["Storage\n(persistence)"]
    EB["EventBus\n(event fan-out)"]
    CR["CancelRegistry\n(cancel signals)"]
    W["Worker\n(your code)"]
    TC["TaskContext\n(execution API)"]

    HTTP -->|requests / responses| TM
    TM -->|schedules tasks| B
    TM -.->|reads / writes| S
    TM -.->|subscribes| EB
    TM -.->|cancel signals| CR
    B -->|delegates execution| WA
    WA -->|"handle(ctx)"| W
    W -.->|uses API| TC
    TC -->|emits via| EE
    EE -->|writes| S
    EE -->|publishes| EB
    WA -.->|checks / cleanup| CR
```

**TaskManager** handles submission, validation, streaming, and cancellation. It coordinates between Broker, Storage, EventBus, and CancelRegistry — but never touches the Worker directly.

**WorkerAdapter** bridges the Broker queue to your Worker. It manages the lifecycle: dequeue → check cancel → build context → transition to `working` → call `handle(ctx)` → cleanup.

**EventEmitter** is the façade that TaskContext uses to persist state (Storage) and broadcast events (EventBus) without knowing about either directly. Storage writes are authoritative; EventBus is best-effort.

**Pluggable backends:** Swap `Storage`, `Broker`, `EventBus`, and `CancelRegistry` independently — e.g. PostgreSQL storage + Redis broker + Redis event bus. All backends implement their respective ABC.

## SkillConfig

Define agent skills without importing A2A types:

```python
from a2akit import A2AServer, AgentCardConfig, SkillConfig, Worker

server = A2AServer(
    worker=MyWorker(),
    agent_card=AgentCardConfig(
        name="My Agent",
        description="Does things",
        skills=[
            SkillConfig(
                id="translate",
                name="Translate",
                description="Translates text between languages",
                tags=["translation", "language"],
                examples=["Translate 'hello' to French"],
            ),
        ],
    ),
)
```

## A2A Endpoints (auto-registered)

| Endpoint | Method | Description |
| --- | --- | --- |
| `/v1/message:send` | POST | Submit task (blocking) |
| `/v1/message:stream` | POST | Submit task (SSE stream) |
| `/v1/tasks/{id}` | GET | Get task by ID |
| `/v1/tasks` | GET | List tasks (filterable, paginated) |
| `/v1/tasks/{id}:cancel` | POST | Cancel a task |
| `/v1/tasks/{id}:subscribe` | POST | Subscribe to task updates (SSE) |
| `/.well-known/agent-card.json` | GET | Agent discovery card |
| `/v1/health` | GET | Health check |

## Custom Backends

Implement `Storage`, `Broker`, `EventBus`, or `CancelRegistry` ABCs and pass them to `A2AServer`:

```python
from a2akit import A2AServer, AgentCardConfig

server = A2AServer(
    worker=MyWorker(),
    agent_card=AgentCardConfig(name="My Agent", description="Does things"),
    storage=MyPostgresStorage(dsn="..."),
    broker=MyRedisBroker(url="redis://..."),
    event_bus=MyRedisEventBus(url="redis://..."),
)
```

All four backends are independently swappable. The in-memory defaults (`"memory"`) are suitable for development and single-process deployments.
