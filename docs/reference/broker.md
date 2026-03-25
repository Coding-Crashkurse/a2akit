# Broker

The Broker is the task queue that schedules and delivers run operations to the WorkerAdapter.

## Broker ABC

::: a2akit.broker.base.Broker
    options:
      members:
        - run_task
        - shutdown
        - receive_task_operations

### `run_task(params, *, is_new_task=False, request_context=None)`

Enqueue a task for execution. Called by TaskManager after submission.

### `shutdown()`

Signal the broker to stop receiving operations. `receive_task_operations()` should terminate gracefully after this is called.

### `receive_task_operations()`

Async generator that yields `OperationHandle` instances from the queue. Runs indefinitely until the broker is shut down.

**Task-level serialization:** Implementations for distributed deployments MUST ensure that at most one operation per `task_id` is in processing at any time.

## InMemoryBroker

The default broker for development. Uses an `asyncio.Queue`.

```python
from a2akit import A2AServer

server = A2AServer(
    worker=MyWorker(),
    agent_card=AgentCardConfig(...),
    broker="memory",  # default
)
```

## RedisBroker

Redis Streams-backed broker for horizontally scalable, multi-process deployments. Uses a single consumer group so each message is delivered to exactly one consumer. Stale messages from dead consumers are periodically reclaimed via `XAUTOCLAIM`.

```python
from a2akit import A2AServer

server = A2AServer(
    worker=MyWorker(),
    agent_card=AgentCardConfig(...),
    broker="redis://localhost:6379/0",
)
```

Or with an explicit instance for advanced configuration:

```python
from a2akit.broker.redis import RedisBroker

broker = RedisBroker(
    "redis://localhost:6379/0",
    stream_name="my-tasks",
    group_name="my-workers",
    block_ms=5000,
    claim_timeout_ms=60000,
)

server = A2AServer(worker=MyWorker(), agent_card=..., broker=broker)
```

Requires `pip install a2akit[redis]`.

### RedisCancelRegistry

When you pass a Redis URL as `broker`, the cancel registry is automatically created as `RedisCancelRegistry` (unless you explicitly provide one). It uses SET keys for durability + Pub/Sub channels for real-time notification.

```python
from a2akit.broker.redis import RedisCancelRegistry

cancel_registry = RedisCancelRegistry("redis://localhost:6379/0", ttl_s=86400)
server = A2AServer(..., cancel_registry=cancel_registry)
```

## OperationHandle

Handle for acknowledging or rejecting a broker operation.

::: a2akit.broker.base.OperationHandle
    options:
      members:
        - operation
        - attempt
        - ack
        - nack

| Property/Method | Description |
|-----------------|-------------|
| `operation` | The wrapped `TaskOperation` |
| `attempt` | Delivery attempt number (1-based) |
| `ack()` | Acknowledge successful processing |
| `nack(*, delay_seconds=0)` | Reject — return to queue for retry |

## CancelRegistry

Registry for task cancellation signals.

::: a2akit.broker.base.CancelRegistry
    options:
      members:
        - request_cancel
        - is_cancelled
        - on_cancel
        - cleanup

| Method | Description |
|--------|-------------|
| `request_cancel(task_id)` | Signal cancellation for a task |
| `is_cancelled(task_id)` | Check if cancellation was requested |
| `on_cancel(task_id)` | Return a `CancelScope` for cooperative checks |
| `cleanup(task_id)` | Release resources (must be idempotent) |

## CancelScope

Backend-agnostic cancellation handle.

```python
class CancelScope(ABC):
    async def wait(self) -> None: ...   # Block until cancelled
    def is_set(self) -> bool: ...       # Non-blocking check
```

## Retry Semantics

The WorkerAdapter uses `OperationHandle.attempt` to decide between retry and terminal failure:

- If `attempt < max_retries`: call `nack(delay_seconds=...)` for exponential back-off
- If `attempt >= max_retries`: `ack()` + mark task as failed

The `InMemoryBroker` always returns `attempt=1`. Queue backends with retry tracking (RabbitMQ, Redis) report the actual delivery count.
