"""JSON-RPC 2.0 transport for A2A protocol."""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from a2a.types import AgentCard, Message, MessageSendParams, Task

from a2akit.client.errors import (
    A2AClientError,
    ProtocolError,
    TaskNotCancelableError,
    TaskNotFoundError,
    TaskTerminalError,
)
from a2akit.client.transport._sse import parse_sse_stream
from a2akit.client.transport.base import Transport

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from a2akit.client.result import StreamEvent

A2A_VERSION = "0.3.0"

# JSON-RPC / A2A error codes → exception mapping
_ERROR_MAP: dict[int, type[A2AClientError]] = {
    -32001: TaskNotFoundError,
    -32002: TaskNotCancelableError,
    -32004: TaskTerminalError,
}


class JsonRpcTransport(Transport):
    """JSON-RPC 2.0 transport implementation."""

    def __init__(self, http_client: httpx.AsyncClient, url: str) -> None:
        self._http = http_client
        self._url = url.rstrip("/") + "/"

    def _headers(self) -> dict[str, str]:
        headers = {"A2A-Version": A2A_VERSION, "Content-Type": "application/json"}
        from a2akit.telemetry._client import inject_trace_context

        inject_trace_context(headers)
        return headers

    def _envelope(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        env: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
        }
        if params is not None:
            env["params"] = params
        return env

    def _handle_response(self, data: dict[str, Any], task_id: str | None = None) -> Any:
        """Extract result or raise typed exception from JSON-RPC response."""
        if "error" in data:
            err = data["error"]
            code = err.get("code", 0)
            message = err.get("message", "Unknown error")

            exc_cls = _ERROR_MAP.get(code)
            if exc_cls is not None:
                if exc_cls is TaskNotFoundError:
                    raise TaskNotFoundError(task_id or "unknown")
                if exc_cls is TaskNotCancelableError:
                    raise TaskNotCancelableError(task_id or "unknown")
                if exc_cls is TaskTerminalError:
                    raise TaskTerminalError(task_id or "unknown")
            if code == -32602:
                raise A2AClientError(message)
            raise ProtocolError(f"JSON-RPC error {code}: {message}")

        return data.get("result")

    async def _call(
        self, method: str, params: dict[str, Any] | None = None, task_id: str | None = None
    ) -> Any:
        """Make a JSON-RPC call and return the result."""
        envelope = self._envelope(method, params)
        try:
            response = await self._http.post(self._url, json=envelope, headers=self._headers())
        except httpx.RequestError as exc:
            raise ProtocolError(f"Request failed: {exc}") from exc

        if not response.is_success:
            raise ProtocolError(f"HTTP {response.status_code}: {response.text}")

        try:
            data = response.json()
        except Exception as exc:
            raise ProtocolError(f"Invalid JSON response: {exc}") from exc

        return self._handle_response(data, task_id=task_id)

    async def send_message(self, params: MessageSendParams) -> Task | Message:
        """JSON-RPC message/send."""
        body = json.loads(params.model_dump_json(by_alias=True, exclude_none=True))
        result = await self._call("message/send", body)
        kind = result.get("kind") if isinstance(result, dict) else None
        if kind == "message":
            return Message.model_validate(result)
        return Task.model_validate(result)

    async def stream_message(self, params: MessageSendParams) -> AsyncIterator[StreamEvent]:
        """JSON-RPC message/sendStream (SSE response)."""
        body = json.loads(params.model_dump_json(by_alias=True, exclude_none=True))
        envelope = self._envelope("message/sendStream", body)
        async with self._http.stream(
            "POST",
            self._url,
            json=envelope,
            headers=self._headers(),
        ) as response:
            if not response.is_success:
                await response.aread()
                raise ProtocolError(f"HTTP {response.status_code}: {response.text}")
            async for event in parse_sse_stream(response, unwrap_jsonrpc=True):
                yield event

    async def get_task(self, task_id: str, history_length: int | None = None) -> Task:
        """JSON-RPC tasks/get."""
        params: dict[str, Any] = {"id": task_id}
        if history_length is not None:
            params["historyLength"] = history_length
        result = await self._call("tasks/get", params, task_id=task_id)
        return Task.model_validate(result)

    async def list_tasks(self, query: dict[str, Any]) -> dict[str, Any]:
        """JSON-RPC tasks/list."""
        result = await self._call("tasks/list", query)
        return result  # type: ignore[no-any-return]

    async def cancel_task(self, task_id: str) -> Task:
        """JSON-RPC tasks/cancel."""
        result = await self._call("tasks/cancel", {"id": task_id}, task_id=task_id)
        return Task.model_validate(result)

    async def subscribe_task(self, task_id: str) -> AsyncIterator[StreamEvent]:
        """JSON-RPC tasks/resubscribe (SSE response)."""
        envelope = self._envelope("tasks/resubscribe", {"id": task_id})
        async with self._http.stream(
            "POST",
            self._url,
            json=envelope,
            headers=self._headers(),
        ) as response:
            if not response.is_success:
                await response.aread()
                raise ProtocolError(f"HTTP {response.status_code}: {response.text}")
            async for event in parse_sse_stream(response, unwrap_jsonrpc=True):
                yield event

    async def set_push_config(self, task_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """JSON-RPC tasks/pushNotificationConfig/set."""
        result = await self._call(
            "tasks/pushNotificationConfig/set",
            {"taskId": task_id, "pushNotificationConfig": config},
            task_id=task_id,
        )
        return result  # type: ignore[no-any-return]

    async def get_push_config(self, task_id: str, config_id: str | None = None) -> dict[str, Any]:
        """JSON-RPC tasks/pushNotificationConfig/get."""
        params: dict[str, Any] = {"id": task_id}
        if config_id:
            params["pushNotificationConfigId"] = config_id
        result = await self._call("tasks/pushNotificationConfig/get", params, task_id=task_id)
        return result  # type: ignore[no-any-return]

    async def list_push_configs(self, task_id: str) -> list[dict[str, Any]]:
        """JSON-RPC tasks/pushNotificationConfig/list."""
        result = await self._call(
            "tasks/pushNotificationConfig/list",
            {"id": task_id},
            task_id=task_id,
        )
        return result  # type: ignore[no-any-return]

    async def delete_push_config(self, task_id: str, config_id: str) -> None:
        """JSON-RPC tasks/pushNotificationConfig/delete."""
        await self._call(
            "tasks/pushNotificationConfig/delete",
            {"id": task_id, "pushNotificationConfigId": config_id},
            task_id=task_id,
        )

    async def get_extended_card(self) -> AgentCard:
        """JSON-RPC agent/getAuthenticatedExtendedCard."""
        result = await self._call("agent/getAuthenticatedExtendedCard")
        return AgentCard.model_validate(result)

    async def close(self) -> None:
        """No-op; HTTP client lifecycle is managed externally."""
