"""Pydantic models for push notification configuration.

Internally modeled on the A2A v1.0 ``TaskPushNotificationConfig`` (flat
shape: ``{taskId, id, url, token, authentication}``) per spec §3.1.7-10.

The v0.3-style nested accessor ``config.push_notification_config.url``
still works — it's a read-only view computed from the flat fields.
Construction accepts both forms so existing user code keeps working::

    # v1.0 flat (preferred)
    TaskPushNotificationConfig(task_id="t", url="http://x", token="tok")

    # v0.3 nested (still supported)
    TaskPushNotificationConfig(
        task_id="t",
        push_notification_config=PushNotificationConfig(url="http://x"),
    )
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator


class PushNotificationAuthenticationInfo(BaseModel):
    """Auth details for the webhook endpoint (A2A §6.9 / v1.0 flat)."""

    schemes: list[str]
    credentials: str | None = None


class PushNotificationConfig(BaseModel):
    """Client-provided webhook configuration (A2A §6.8).

    Retained for backwards compatibility and as the return type of the
    ``push_notification_config`` nested accessor on
    :class:`TaskPushNotificationConfig`.
    """

    id: str | None = None
    url: str
    token: str | None = None
    authentication: PushNotificationAuthenticationInfo | None = None


class TaskPushNotificationConfig(BaseModel):
    """Binds a webhook configuration to a task (A2A v1.0 §3.1.7-10, flat shape).

    Fields match the v1.0 wire format directly; callers reading the legacy
    ``config.push_notification_config.url`` pattern still resolve through
    the :attr:`push_notification_config` property.
    """

    task_id: str = Field(alias="taskId")
    id: str | None = None
    url: str
    token: str | None = None
    authentication: PushNotificationAuthenticationInfo | None = None

    model_config = {"populate_by_name": True}

    @model_validator(mode="before")
    @classmethod
    def _unwrap_nested(cls, data: Any) -> Any:
        """Accept legacy v0.3 ``push_notification_config={...}`` construction."""
        if not isinstance(data, dict):
            return data
        data = dict(data)  # shallow copy — never mutate the caller's dict
        nested = data.pop("push_notification_config", None) or data.pop(
            "pushNotificationConfig", None
        )
        if nested is None:
            return data
        if isinstance(nested, BaseModel):
            nested = nested.model_dump()
        # Flat fields on the outer dict take precedence over the nested ones.
        for key in ("id", "url", "token", "authentication"):
            if key in nested and key not in data:
                data[key] = nested[key]
        return data

    @property
    def push_notification_config(self) -> PushNotificationConfig:
        """Legacy nested view — ``config.push_notification_config.url`` still works."""
        return PushNotificationConfig(
            id=self.id,
            url=self.url,
            token=self.token,
            authentication=self.authentication,
        )
