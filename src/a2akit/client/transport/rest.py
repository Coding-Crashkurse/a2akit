"""HTTP+JSON/REST transport for A2A protocol."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

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

    import httpx

    from a2akit.client.result import StreamEvent

A2A_VERSION = "0.3.0"


class RestTransport(Transport):
    """HTTP+JSON/REST transport implementation."""

    def __init__(self, http_client: httpx.AsyncClient, base_url: str) -> None:
        self._http = http_client
        # base_url should end with /v1
        self._base = base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"A2A-Version": A2A_VERSION}
        from a2akit.telemetry._client import inject_trace_context

        inject_trace_context(headers)
        return headers

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

    def _check_error(self, response: httpx.Response, task_id: str | None = None) -> None:
        """Map HTTP error status codes to typed exceptions."""
        if response.is_success:
            return

        status = response.status_code
        detail = ""
        code: int | None = None
        try:
            body = response.json()
            if isinstance(body, dict):
                raw = body.get("message", body.get("detail", ""))
                if isinstance(raw, dict):
                    code = raw.get("code")
                    detail = str(raw.get("message", raw))
                else:
                    detail = str(raw)
                    code = body.get("code")
        except Exception:
            detail = response.text or f"HTTP {status}"

        tid = task_id or "unknown"
        if status == 404:
            raise TaskNotFoundError(tid)
        if status == 409:
            if code == -32002:
                raise TaskNotCancelableError(tid)
            if code == -32004:
                raise TaskTerminalError(tid)
            raise A2AClientError(str(detail) or "Conflict (HTTP 409)")
        if status == 400:
            raise A2AClientError(str(detail) or "Bad request")
        raise ProtocolError(f"HTTP {status}: {detail}")

    async def send_message(self, params: MessageSendParams) -> Task | Message:
        """POST /v1/message:send."""
        body = json.loads(params.model_dump_json(by_alias=True, exclude_none=True))
        response = await self._http.post(
            self._url("/message:send"),
            json=body,
            headers=self._headers(),
        )
        self._check_error(response)
        data = response.json()
        # Detect direct reply (Message) vs Task by 'kind' field
        kind = data.get("kind")
        if kind == "message":
            return Message.model_validate(data)
        return Task.model_validate(data)

    async def stream_message(self, params: MessageSendParams) -> AsyncIterator[StreamEvent]:
        """POST /v1/message:stream (SSE)."""
        body = json.loads(params.model_dump_json(by_alias=True, exclude_none=True))
        async with self._http.stream(
            "POST",
            self._url("/message:stream"),
            json=body,
            headers=self._headers(),
        ) as response:
            if not response.is_success:
                await response.aread()
                self._check_error(response)
            async for event in parse_sse_stream(response):
                yield event

    async def get_task(self, task_id: str, history_length: int | None = None) -> Task:
        """GET /v1/tasks/{task_id}."""
        params: dict[str, Any] = {}
        if history_length is not None:
            params["historyLength"] = history_length
        response = await self._http.get(
            self._url(f"/tasks/{task_id}"),
            params=params,
            headers=self._headers(),
        )
        self._check_error(response, task_id=task_id)
        return Task.model_validate(response.json())

    async def list_tasks(self, query: dict[str, Any]) -> dict[str, Any]:
        """GET /v1/tasks with query parameters."""
        response = await self._http.get(
            self._url("/tasks"),
            params={k: v for k, v in query.items() if v is not None},
            headers=self._headers(),
        )
        self._check_error(response)
        return response.json()  # type: ignore[no-any-return]

    async def cancel_task(self, task_id: str) -> Task:
        """POST /v1/tasks/{task_id}:cancel."""
        response = await self._http.post(
            self._url(f"/tasks/{task_id}:cancel"),
            headers=self._headers(),
        )
        self._check_error(response, task_id=task_id)
        return Task.model_validate(response.json())

    async def subscribe_task(self, task_id: str) -> AsyncIterator[StreamEvent]:
        """POST /v1/tasks/{task_id}:subscribe (SSE)."""
        async with self._http.stream(
            "POST",
            self._url(f"/tasks/{task_id}:subscribe"),
            headers=self._headers(),
        ) as response:
            if not response.is_success:
                await response.aread()
                self._check_error(response, task_id=task_id)
            async for event in parse_sse_stream(response):
                yield event

    async def set_push_config(self, task_id: str, config: dict[str, Any]) -> dict[str, Any]:
        """POST /v1/tasks/{task_id}/pushNotificationConfigs."""
        response = await self._http.post(
            self._url(f"/tasks/{task_id}/pushNotificationConfigs"),
            json=config,
            headers=self._headers(),
        )
        self._check_error(response, task_id=task_id)
        return response.json()  # type: ignore[no-any-return]

    async def get_push_config(self, task_id: str, config_id: str | None = None) -> dict[str, Any]:
        """GET /v1/tasks/{task_id}/pushNotificationConfigs/{configId}."""
        path = f"/tasks/{task_id}/pushNotificationConfigs"
        if config_id:
            path = f"/tasks/{task_id}/pushNotificationConfigs/{config_id}"
        response = await self._http.get(
            self._url(path),
            headers=self._headers(),
        )
        self._check_error(response, task_id=task_id)
        return response.json()  # type: ignore[no-any-return]

    async def list_push_configs(self, task_id: str) -> list[dict[str, Any]]:
        """GET /v1/tasks/{task_id}/pushNotificationConfigs."""
        response = await self._http.get(
            self._url(f"/tasks/{task_id}/pushNotificationConfigs"),
            headers=self._headers(),
        )
        self._check_error(response, task_id=task_id)
        return response.json()  # type: ignore[no-any-return]

    async def delete_push_config(self, task_id: str, config_id: str) -> None:
        """DELETE /v1/tasks/{task_id}/pushNotificationConfigs/{configId}."""
        response = await self._http.delete(
            self._url(f"/tasks/{task_id}/pushNotificationConfigs/{config_id}"),
            headers=self._headers(),
        )
        self._check_error(response, task_id=task_id)

    async def get_extended_card(self) -> AgentCard:
        """GET /v1/card — fetch the authenticated extended agent card."""
        response = await self._http.get(
            self._url("/card"),
            headers=self._headers(),
        )
        self._check_error(response)
        return AgentCard.model_validate(response.json())

    async def close(self) -> None:
        """No-op; HTTP client lifecycle is managed externally."""
