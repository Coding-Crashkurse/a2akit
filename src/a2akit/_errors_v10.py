"""google.rpc.Status error responses for the A2A v1.0 wire.

Spec §5.1 / §20: v1.0 replaces the v0.3 ``{"code": -32001, "message": ...}``
shape with a Google-RPC-style envelope::

    {
        "error": {
            "code": 404,
            "status": "NOT_FOUND",
            "message": "The specified task ID does not exist",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "TASK_NOT_FOUND",
                    "domain": "a2a-protocol.org",
                    "metadata": {"taskId": "abc"},
                }
            ],
        }
    }

One central exception → descriptor mapping feeds REST, JSON-RPC, and (when
it ships) gRPC so all three transports emit consistent error codes / reasons.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi.responses import JSONResponse

from a2akit.errors import AuthenticationRequiredError
from a2akit.push.endpoints import PushConfigNotFoundError
from a2akit.storage.base import (
    ConcurrencyError,
    ContentTypeNotSupportedError,
    ContextMismatchError,
    InvalidAgentResponseError,
    TaskNotAcceptingMessagesError,
    TaskNotCancelableError,
    TaskNotFoundError,
    TaskTerminalStateError,
    UnsupportedOperationError,
)

_ERROR_INFO_TYPE = "type.googleapis.com/google.rpc.ErrorInfo"
_ERROR_DOMAIN = "a2a-protocol.org"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ErrorDescriptor:
    """Canonical mapping for one framework exception to its v1.0 error shape."""

    http_status: int
    grpc_status: str  # "NOT_FOUND", "FAILED_PRECONDITION", etc.
    json_rpc_code: int
    reason: str  # machine-readable "TASK_NOT_FOUND", …
    default_message: str


# Sentinels for error shapes that aren't tied to a single framework exception.
VALIDATION_ERROR = ErrorDescriptor(
    http_status=400,
    grpc_status="INVALID_ARGUMENT",
    json_rpc_code=-32600,
    reason="INVALID_REQUEST",
    default_message="Invalid request",
)

METHOD_NOT_FOUND = ErrorDescriptor(
    http_status=404,
    grpc_status="NOT_FOUND",
    json_rpc_code=-32601,
    reason="METHOD_NOT_FOUND",
    default_message="Method not found",
)

PARSE_ERROR = ErrorDescriptor(
    http_status=400,
    grpc_status="INVALID_ARGUMENT",
    json_rpc_code=-32700,
    reason="PARSE_ERROR",
    default_message="Parse error",
)

INTERNAL_ERROR = ErrorDescriptor(
    http_status=500,
    grpc_status="INTERNAL",
    json_rpc_code=-32603,
    reason="INTERNAL_ERROR",
    default_message="Internal error",
)


ERROR_CATALOG: dict[type[Exception], ErrorDescriptor] = {
    TaskNotFoundError: ErrorDescriptor(
        http_status=404,
        grpc_status="NOT_FOUND",
        json_rpc_code=-32001,
        reason="TASK_NOT_FOUND",
        default_message="Task not found",
    ),
    TaskTerminalStateError: ErrorDescriptor(
        http_status=409,
        grpc_status="FAILED_PRECONDITION",
        json_rpc_code=-32004,
        reason="TASK_TERMINAL_STATE",
        default_message="Task is in terminal state",
    ),
    TaskNotCancelableError: ErrorDescriptor(
        http_status=409,
        grpc_status="FAILED_PRECONDITION",
        json_rpc_code=-32002,
        reason="TASK_NOT_CANCELABLE",
        default_message="Task is not cancelable",
    ),
    ContextMismatchError: ErrorDescriptor(
        http_status=400,
        grpc_status="INVALID_ARGUMENT",
        json_rpc_code=-32602,
        reason="CONTEXT_MISMATCH",
        default_message="contextId does not match task",
    ),
    TaskNotAcceptingMessagesError: ErrorDescriptor(
        http_status=422,
        grpc_status="FAILED_PRECONDITION",
        json_rpc_code=-32602,
        reason="TASK_NOT_ACCEPTING_MESSAGES",
        default_message="Task does not accept messages",
    ),
    UnsupportedOperationError: ErrorDescriptor(
        http_status=400,
        grpc_status="UNIMPLEMENTED",
        json_rpc_code=-32004,
        reason="UNSUPPORTED_OPERATION",
        default_message="Operation not supported",
    ),
    ContentTypeNotSupportedError: ErrorDescriptor(
        http_status=415,
        grpc_status="INVALID_ARGUMENT",
        json_rpc_code=-32005,
        reason="CONTENT_TYPE_NOT_SUPPORTED",
        default_message="Incompatible content type",
    ),
    InvalidAgentResponseError: ErrorDescriptor(
        http_status=500,
        grpc_status="INTERNAL",
        json_rpc_code=-32006,
        reason="INVALID_AGENT_RESPONSE",
        default_message="Invalid agent response",
    ),
    ConcurrencyError: ErrorDescriptor(
        http_status=409,
        grpc_status="ABORTED",
        json_rpc_code=-32004,
        reason="CONCURRENT_MODIFICATION",
        default_message="Concurrent modification, please retry",
    ),
    AuthenticationRequiredError: ErrorDescriptor(
        http_status=401,
        grpc_status="UNAUTHENTICATED",
        json_rpc_code=-32603,
        reason="AUTH_REQUIRED",
        default_message="Authentication required",
    ),
    PushConfigNotFoundError: ErrorDescriptor(
        http_status=404,
        grpc_status="NOT_FOUND",
        json_rpc_code=-32001,
        reason="PUSH_CONFIG_NOT_FOUND",
        default_message="Push config not found",
    ),
}


def descriptor_for(exc: Exception) -> ErrorDescriptor:
    """Return the catalog entry for an exception, or ``INTERNAL_ERROR``.

    Walks ``type(exc).__mro__`` so subclasses of cataloged exceptions map
    to their base's descriptor — mirroring the ``isinstance`` semantics of
    the registered FastAPI exception handlers.
    """
    for klass in type(exc).__mro__:
        found = ERROR_CATALOG.get(klass)
        if found is not None:
            return found
    return INTERNAL_ERROR


@dataclass
class _V10ErrorBody:
    """In-memory representation of the google.rpc.Status envelope."""

    http_status: int
    grpc_status: str
    message: str
    reason: str
    metadata: dict[str, str] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        details: list[dict[str, Any]] = [
            {
                "@type": _ERROR_INFO_TYPE,
                "reason": self.reason,
                "domain": _ERROR_DOMAIN,
            }
        ]
        if self.metadata:
            details[0]["metadata"] = self.metadata
        return {
            "error": {
                "code": self.http_status,
                "status": self.grpc_status,
                "message": self.message,
                "details": details,
            }
        }


def build_error(
    *,
    http_status: int,
    grpc_status: str,
    message: str,
    reason: str,
    metadata: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Produce a ``JSONResponse`` in google.rpc.Status shape."""
    body = _V10ErrorBody(
        http_status=http_status,
        grpc_status=grpc_status,
        message=message,
        reason=reason,
        metadata=metadata or {},
    )
    return JSONResponse(
        status_code=http_status,
        content=body.to_payload(),
        headers=headers,
    )


def build_error_from_exception(
    exc: Exception,
    *,
    message: str | None = None,
    metadata: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Look up the descriptor for ``exc`` and build the matching response.

    Exception-specific metadata (``ContentTypeNotSupportedError.mime_type``
    for example) gets folded into ``metadata`` automatically.
    """
    desc = descriptor_for(exc)
    if desc is INTERNAL_ERROR:
        # Uncataloged exception: never echo str(exc) to the client — it may
        # leak internals. Log the detail server-side instead.
        logger.error("Unhandled exception on the v1.0 wire", exc_info=exc)
        message = INTERNAL_ERROR.default_message
    merged_meta: dict[str, str] = {}
    if metadata:
        merged_meta.update(metadata)
    if isinstance(exc, ContentTypeNotSupportedError):
        merged_meta.setdefault("mimeType", exc.mime_type)
    if isinstance(exc, InvalidAgentResponseError):
        merged_meta.setdefault("detail", exc.detail)
    if isinstance(exc, AuthenticationRequiredError):
        headers = {**(headers or {}), "WWW-Authenticate": f'{exc.scheme} realm="{exc.realm}"'}
    return build_error(
        http_status=desc.http_status,
        grpc_status=desc.grpc_status,
        message=message or str(exc) or desc.default_message,
        reason=desc.reason,
        metadata=merged_meta,
        headers=headers,
    )


def jsonrpc_error_from_exception(exc: Exception, req_id: Any) -> dict[str, Any]:
    """Build a JSON-RPC-2.0 error envelope carrying the google.rpc.ErrorInfo.

    v1.0 spec §5.2: the outer ``error.code`` stays as the existing JSON-RPC
    numeric code for back-compat with transports that only know that shape,
    while ``error.data`` carries the ``google.rpc.ErrorInfo`` entry (list form
    per spec — same shape REST emits under ``details``).
    """
    desc = descriptor_for(exc)
    if desc is INTERNAL_ERROR:
        # Uncataloged exception: never echo str(exc) to the client — it may
        # leak internals. Log the detail server-side instead.
        logger.error("Unhandled exception in v1.0 JSON-RPC handler", exc_info=exc)
        message = INTERNAL_ERROR.default_message
    else:
        message = str(exc) or desc.default_message
    metadata: dict[str, str] = {}
    if isinstance(exc, ContentTypeNotSupportedError):
        metadata["mimeType"] = exc.mime_type
    if isinstance(exc, InvalidAgentResponseError):
        metadata["detail"] = exc.detail
    info: dict[str, Any] = {
        "@type": _ERROR_INFO_TYPE,
        "reason": desc.reason,
        "domain": _ERROR_DOMAIN,
    }
    if metadata:
        info["metadata"] = metadata
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {
            "code": desc.json_rpc_code,
            "message": message,
            "data": [info],
        },
    }


__all__ = [
    "ERROR_CATALOG",
    "INTERNAL_ERROR",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "VALIDATION_ERROR",
    "ErrorDescriptor",
    "build_error",
    "build_error_from_exception",
    "descriptor_for",
    "jsonrpc_error_from_exception",
]
