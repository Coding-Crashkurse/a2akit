"""Task submission, streaming, querying, and cancellation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from a2a.types import MessageSendParams, Task, TaskStatusUpdateEvent

from agentserve.broker import Broker, CancelRegistry
from agentserve.event_bus.base import EventBus
from agentserve.schema import StreamEvent
from agentserve.storage import Storage
from agentserve.storage.base import TERMINAL_STATES, ListTasksQuery, ListTasksResult

logger = logging.getLogger(__name__)


@dataclass
class TaskManager:
    """High-level API for submitting, streaming, and managing tasks."""

    broker: Broker
    storage: Storage
    event_bus: EventBus
    cancel_registry: CancelRegistry
    default_blocking_timeout_s: float = 30.0
    _background_tasks: set[asyncio.Task[Any]] = field(
        default_factory=set, init=False, repr=False
    )

    async def send_message(self, params: MessageSendParams) -> Task:
        """Submit a task and optionally block until completion."""
        msg = params.message
        is_new = not msg.task_id
        context_id = msg.context_id or str(uuid.uuid4())
        task = await self.storage.submit_task(context_id, msg)

        params.message.context_id = context_id
        params.message.task_id = task.id

        fut = asyncio.create_task(self.broker.run_task(params, is_new_task=is_new))
        self._background_tasks.add(fut)
        fut.add_done_callback(self._background_tasks.discard)

        if params.configuration and params.configuration.blocking:
            try:
                async with asyncio.timeout(self.default_blocking_timeout_s):
                    async for ev in await self.event_bus.subscribe(task.id):
                        if isinstance(ev, TaskStatusUpdateEvent) and ev.final:
                            break
            except TimeoutError:
                logger.info("Blocking wait timed out for task %s", task.id)

        history_len = getattr(
            getattr(params, "configuration", None), "history_length", None
        )
        latest = await self.storage.load_task(task.id, history_length=history_len)
        return latest or task

    async def stream_message(
        self, params: MessageSendParams
    ) -> AsyncGenerator[StreamEvent, None]:
        """Submit a task, yield initial snapshot, then stream live events."""
        msg = params.message
        is_new = not msg.task_id
        context_id = msg.context_id or str(uuid.uuid4())
        task = await self.storage.submit_task(context_id, msg)
        yield task

        params.message.context_id = context_id
        params.message.task_id = task.id

        fut = asyncio.create_task(self.broker.run_task(params, is_new_task=is_new))
        self._background_tasks.add(fut)
        fut.add_done_callback(self._background_tasks.discard)

        async for ev in await self.event_bus.subscribe(task.id):
            yield ev

    async def subscribe_task(self, task_id: str) -> AsyncGenerator[StreamEvent, None]:
        """Subscribe to updates for an existing task.

        Yields the current task state first, then streams live events.
        """
        task = await self.storage.load_task(task_id)
        if task is None:
            from agentserve.storage.base import TaskNotFoundError

            raise TaskNotFoundError(f"Task {task_id} not found")
        if task.status.state in TERMINAL_STATES:
            yield task
            return
        yield task
        async for ev in await self.event_bus.subscribe(task_id):
            yield ev

    async def get_task(
        self, task_id: str, history_length: int | None = None
    ) -> Task | None:
        """Load a single task by ID."""
        return await self.storage.load_task(task_id, history_length)

    async def list_tasks(self, query: ListTasksQuery) -> ListTasksResult:
        """Return filtered and paginated tasks."""
        return await self.storage.list_tasks(query)

    async def cancel_task(self, task_id: str) -> Task | None:
        """Request cancellation of a task and return its current state.

        Signals the cancel registry. The worker is responsible
        for the actual state transition to avoid races with concurrent
        complete/fail calls.
        """
        await self.cancel_registry.request_cancel(task_id)
        return await self.storage.load_task(task_id)
