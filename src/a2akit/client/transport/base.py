"""Abstract base class for A2A client transports."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from a2a.types import AgentCard, Message, MessageSendParams, Task

    from a2akit.client.result import StreamEvent


class Transport(ABC):
    """Internal transport abstraction for A2A protocol bindings."""

    @abstractmethod
    async def send_message(self, params: MessageSendParams) -> Task | Message: ...

    @abstractmethod
    def stream_message(self, params: MessageSendParams) -> AsyncIterator[StreamEvent]: ...

    @abstractmethod
    async def get_task(self, task_id: str, history_length: int | None = None) -> Task: ...

    @abstractmethod
    async def list_tasks(self, query: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    async def cancel_task(self, task_id: str) -> Task: ...

    @abstractmethod
    def subscribe_task(self, task_id: str) -> AsyncIterator[StreamEvent]: ...

    @abstractmethod
    async def set_push_config(self, task_id: str, config: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    async def get_push_config(
        self, task_id: str, config_id: str | None = None
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def list_push_configs(self, task_id: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def delete_push_config(self, task_id: str, config_id: str) -> None: ...

    @abstractmethod
    async def get_extended_card(self) -> AgentCard: ...

    @abstractmethod
    async def close(self) -> None: ...
