"""A2AClient — dev-first client for interacting with A2A agents."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, Self, TypeVar

import httpx
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    Message,
    MessageSendConfiguration,
    MessageSendParams,
    Part,
    Role,
    Task,
    TextPart,
    TransportProtocol,
)

from a2akit.client.errors import (
    AgentCapabilityError,
    AgentNotFoundError,
    NotConnectedError,
)
from a2akit.client.result import ClientResult, ListResult, StreamEvent
from a2akit.client.transport.jsonrpc import JsonRpcTransport
from a2akit.client.transport.rest import RestTransport
from a2akit.telemetry._client import traced_client_method
from a2akit.telemetry._semantic import (
    SPAN_CLIENT_CANCEL,
    SPAN_CLIENT_CONNECT,
    SPAN_CLIENT_GET_TASK,
    SPAN_CLIENT_LIST_TASKS,
    SPAN_CLIENT_SEND,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from a2akit.client.transport.base import Transport

_T = TypeVar("_T")


class A2AClient:
    """Client for interacting with A2A protocol agents.

    Usage::

        async with A2AClient("http://localhost:8000") as client:
            result = await client.send("Hello!")
            print(result.text)
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        protocol: str | None = None,
        httpx_client: httpx.AsyncClient | None = None,
        card_validator: Callable[[AgentCard, bytes], None] | None = None,
        max_retries: int = 0,
        retry_delay: float = 1.0,
        retry_on: tuple[type[Exception], ...] = (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
        ),
    ) -> None:
        self._url = url.rstrip("/")
        self._headers = headers or {}
        self._timeout = timeout
        self._protocol_preference = protocol
        self._external_http = httpx_client is not None
        self._http_client = httpx_client
        self._card_validator = card_validator
        self._agent_card: AgentCard | None = None
        self._transport: Transport | None = None
        self._connected = False
        self._active_protocol: str = ""
        # Retry config
        self._max_retries = max_retries
        self._retry_delay = retry_delay
        self._retry_on = retry_on

    @traced_client_method(SPAN_CLIENT_CONNECT)
    async def connect(self) -> None:
        """Discover agent and prepare transport.

        Implements transport fallback (Spec §5.6.3): if the preferred
        transport fails a health check, tries ``additionalInterfaces``
        from the agent card before giving up.
        """
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                headers=self._headers,
                timeout=self._timeout,
            )

        card_url = f"{self._url}/.well-known/agent-card.json"
        try:
            resp = await self._http_client.get(card_url)
        except httpx.RequestError as exc:
            raise AgentNotFoundError(self._url, f"Connection failed: {exc}") from exc

        if resp.status_code != 200:
            raise AgentNotFoundError(self._url, f"HTTP {resp.status_code}")

        try:
            card_data = resp.json()
            self._agent_card = AgentCard.model_validate(card_data)
        except Exception as exc:
            raise AgentNotFoundError(self._url, f"Invalid agent card: {exc}") from exc

        if self._card_validator is not None:
            self._card_validator(self._agent_card, resp.content)

        # Build transport candidates: preferred first, then additionalInterfaces
        candidates = self._build_transport_candidates(self._agent_card)

        errors: list[tuple[str, str, Exception]] = []
        for url, proto in candidates:
            try:
                transport = self._create_transport(proto, url)
                await transport.health_check()
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                errors.append((url, proto, exc))
                continue
            except Exception:
                # Health check might not exist or might fail for other reasons;
                # accept the transport anyway (backwards compat).
                pass

            self._transport = transport
            self._active_protocol = proto
            self._connected = True
            return

        # All candidates failed — only happens when all raised connect errors
        if errors:
            detail = "; ".join(f"{proto}@{url}: {exc}" for url, proto, exc in errors)
            raise AgentNotFoundError(self._url, f"All transports failed: {detail}")

        # Fallback: should not reach here, but just in case
        raise AgentNotFoundError(self._url, "No suitable transport found")

    def _build_transport_candidates(self, card: AgentCard) -> list[tuple[str, str]]:
        """Build ordered list of (url, protocol) transport candidates."""
        candidates: list[tuple[str, str]] = []

        # Preferred transport first
        preferred_proto = self._detect_protocol(card)
        candidates.append((card.url, preferred_proto))

        # Additional interfaces as fallback
        for iface in card.additional_interfaces or []:
            proto = self._protocol_from_transport(iface.transport)
            if proto:
                entry = (iface.url or card.url, proto)
                if entry not in candidates:
                    candidates.append(entry)

        return candidates

    @staticmethod
    def _protocol_from_transport(transport: Any) -> str | None:
        """Map a TransportProtocol to our internal protocol string."""
        if isinstance(transport, TransportProtocol):
            val = transport.value
        else:
            val = str(transport)

        val_lower = val.lower()
        if "http" in val_lower or ("json" in val_lower and "rpc" not in val_lower):
            return "http+json"
        if "jsonrpc" in val_lower or "rpc" in val_lower:
            return "jsonrpc"
        return None

    def _create_transport(self, proto: str, url: str) -> Transport:
        """Create a transport instance for the given protocol."""
        assert self._http_client is not None
        if proto == "http+json":
            return RestTransport(self._http_client, url)
        return JsonRpcTransport(self._http_client, url)

    def _detect_protocol(self, card: AgentCard) -> str:
        """Determine which protocol to use."""
        if self._protocol_preference:
            return self._protocol_preference

        if card.preferred_transport is not None:
            result = self._protocol_from_transport(card.preferred_transport)
            if result:
                return result

        return "jsonrpc"

    async def _with_retry(self, coro_factory: Callable[[], Awaitable[_T]]) -> _T:
        """Execute with exponential backoff retries on network errors."""
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await coro_factory()
            except self._retry_on as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    await asyncio.sleep(self._retry_delay * (2**attempt))
        raise last_exc  # type: ignore[misc]

    async def close(self) -> None:
        """Clean up resources."""
        if self._transport is not None:
            await self._transport.close()
            self._transport = None
        if self._http_client is not None and not self._external_http:
            await self._http_client.aclose()
            self._http_client = None
        self._connected = False

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    def _ensure_connected(self) -> Transport:
        """Return transport or raise NotConnectedError."""
        if not self._connected or self._transport is None:
            raise NotConnectedError
        return self._transport

    @property
    def agent_card(self) -> AgentCard:
        """The discovered agent card."""
        if self._agent_card is None:
            raise NotConnectedError
        return self._agent_card

    @property
    def agent_name(self) -> str:
        """Shortcut for agent_card.name."""
        return self.agent_card.name

    @property
    def capabilities(self) -> AgentCapabilities | None:
        """Shortcut for agent_card.capabilities."""
        return self.agent_card.capabilities

    @property
    def protocol(self) -> str:
        """The active protocol ('jsonrpc' or 'http+json')."""
        return self._active_protocol

    @property
    def is_connected(self) -> bool:
        """Whether the client is connected."""
        return self._connected

    async def send(
        self,
        text: str,
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        blocking: bool = True,
        metadata: dict[str, Any] | None = None,
        push_url: str | None = None,
        push_token: str | None = None,
    ) -> ClientResult:
        """Send a text message to the agent.

        If ``push_url`` is provided, a push notification config is created
        inline with the message submission.
        """
        parts = [Part(root=TextPart(text=text))]
        result = await self.send_parts(
            parts,
            task_id=task_id,
            context_id=context_id,
            blocking=blocking,
            metadata=metadata,
        )

        # Inline push config convenience
        if push_url and result.task_id:
            await self.set_push_config(result.task_id, url=push_url, token=push_token)

        return result

    @traced_client_method(SPAN_CLIENT_SEND)
    async def send_parts(
        self,
        parts: list[Part],
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        blocking: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> ClientResult:
        """Send raw Part objects to the agent."""
        transport = self._ensure_connected()
        params = self._build_params(
            parts,
            task_id=task_id,
            context_id=context_id,
            blocking=blocking,
            metadata=metadata,
        )

        async def _do_send() -> Task | Message:
            return await transport.send_message(params)

        result = await self._with_retry(_do_send) if self._max_retries > 0 else await _do_send()
        if isinstance(result, Message):
            return ClientResult.from_message(result)
        return ClientResult.from_task(result)

    async def stream(
        self,
        text: str,
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream a message to the agent, yielding events."""
        transport = self._ensure_connected()
        card = self.agent_card
        if not card.capabilities or not card.capabilities.streaming:
            raise AgentCapabilityError(card.name, "streaming")

        parts = [Part(root=TextPart(text=text))]
        params = self._build_params(
            parts,
            task_id=task_id,
            context_id=context_id,
            blocking=False,
            metadata=metadata,
        )
        async for event in transport.stream_message(params):
            yield event

    async def stream_text(
        self,
        text: str,
        *,
        task_id: str | None = None,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """Stream only text content — yields plain strings."""
        async for event in self.stream(
            text, task_id=task_id, context_id=context_id, metadata=metadata
        ):
            if event.kind == "artifact" and event.text:
                yield event.text

    @traced_client_method(SPAN_CLIENT_GET_TASK)
    async def get_task(
        self,
        task_id: str,
        *,
        history_length: int | None = None,
    ) -> ClientResult:
        """Fetch a task by ID."""
        transport = self._ensure_connected()

        async def _do_get() -> Task:
            return await transport.get_task(task_id, history_length)

        task = await self._with_retry(_do_get) if self._max_retries > 0 else await _do_get()
        return ClientResult.from_task(task)

    @traced_client_method(SPAN_CLIENT_LIST_TASKS)
    async def list_tasks(
        self,
        *,
        context_id: str | None = None,
        status: str | None = None,
        page_size: int = 50,
        page_token: str | None = None,
        history_length: int | None = None,
    ) -> ListResult:
        """List tasks with optional filters."""
        transport = self._ensure_connected()
        query: dict[str, Any] = {"pageSize": page_size}
        if context_id is not None:
            query["contextId"] = context_id
        if status is not None:
            query["status"] = status
        if page_token is not None:
            query["pageToken"] = page_token
        if history_length is not None:
            query["historyLength"] = history_length

        async def _do_list() -> dict[str, Any]:
            return await transport.list_tasks(query)

        raw = await self._with_retry(_do_list) if self._max_retries > 0 else await _do_list()
        tasks_data = raw.get("tasks", []) if isinstance(raw, dict) else []
        results = [ClientResult.from_task(Task.model_validate(t)) for t in tasks_data]
        return ListResult(
            tasks=results,
            next_page_token=raw.get("nextPageToken") if isinstance(raw, dict) else None,
            total_size=raw.get("totalSize") if isinstance(raw, dict) else None,
            page_size=page_size,
        )

    @traced_client_method(SPAN_CLIENT_CANCEL)
    async def cancel(self, task_id: str) -> ClientResult:
        """Cancel a task by ID."""
        transport = self._ensure_connected()

        async def _do_cancel() -> Task:
            return await transport.cancel_task(task_id)

        task = await self._with_retry(_do_cancel) if self._max_retries > 0 else await _do_cancel()
        return ClientResult.from_task(task)

    async def subscribe(
        self,
        task_id: str,
        *,
        last_event_id: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Subscribe to updates for an existing task.

        Args:
            task_id: The task to subscribe to.
            last_event_id: Resume from this event ID (SSE Last-Event-ID replay).
        """
        transport = self._ensure_connected()
        card = self.agent_card
        if not card.capabilities or not card.capabilities.streaming:
            raise AgentCapabilityError(card.name, "streaming")

        async for event in transport.subscribe_task(task_id, last_event_id=last_event_id):
            yield event

    async def set_push_config(
        self,
        task_id: str,
        *,
        url: str,
        token: str | None = None,
        config_id: str | None = None,
        authentication: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Set a push notification config for a task."""
        transport = self._ensure_connected()
        config: dict[str, Any] = {"url": url}
        if token:
            config["token"] = token
        if config_id:
            config["id"] = config_id
        if authentication:
            config["authentication"] = authentication
        return await transport.set_push_config(task_id, config)

    async def get_push_config(
        self,
        task_id: str,
        config_id: str | None = None,
    ) -> dict[str, Any]:
        """Get a push notification config."""
        transport = self._ensure_connected()
        return await transport.get_push_config(task_id, config_id)

    async def list_push_configs(
        self,
        task_id: str,
    ) -> list[dict[str, Any]]:
        """List all push configs for a task."""
        transport = self._ensure_connected()
        return await transport.list_push_configs(task_id)

    async def delete_push_config(
        self,
        task_id: str,
        config_id: str,
    ) -> None:
        """Delete a push notification config."""
        transport = self._ensure_connected()
        await transport.delete_push_config(task_id, config_id)

    async def get_extended_card(self) -> AgentCard:
        """Fetch the authenticated extended agent card."""
        transport = self._ensure_connected()
        return await transport.get_extended_card()

    @staticmethod
    def _build_params(
        parts: list[Part],
        *,
        task_id: str | None,
        context_id: str | None,
        blocking: bool,
        metadata: dict[str, Any] | None,
    ) -> MessageSendParams:
        """Build MessageSendParams from user inputs."""
        msg_kwargs: dict[str, Any] = {
            "role": Role.user,
            "parts": parts,
            "message_id": str(uuid.uuid4()),
        }
        if task_id is not None:
            msg_kwargs["task_id"] = task_id
        if context_id is not None:
            msg_kwargs["context_id"] = context_id
        if metadata is not None:
            msg_kwargs["metadata"] = metadata

        from a2a.types import Message as A2AMessage

        message = A2AMessage(**msg_kwargs)

        params_kwargs: dict[str, Any] = {"message": message}
        if blocking:
            params_kwargs["configuration"] = MessageSendConfiguration(blocking=True)

        return MessageSendParams(**params_kwargs)
