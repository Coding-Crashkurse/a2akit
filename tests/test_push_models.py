"""Tests for push notification config models."""

from __future__ import annotations

import copy

from a2akit.push.models import PushNotificationConfig, TaskPushNotificationConfig


def test_validate_nested_dict_does_not_mutate_input():
    """The mode="before" unwrap validator must not pop keys out of the
    caller's dict — re-validating the same payload must keep working."""
    payload = {
        "taskId": "task-1",
        "pushNotificationConfig": {"url": "https://example.com/webhook", "token": "tok"},
    }
    snapshot = copy.deepcopy(payload)

    first = TaskPushNotificationConfig.model_validate(payload)
    assert payload == snapshot  # caller's dict untouched

    second = TaskPushNotificationConfig.model_validate(payload)  # re-validate same dict
    assert second == first
    assert second.url == "https://example.com/webhook"
    assert second.token == "tok"


def test_validate_snake_case_nested_does_not_mutate_input():
    payload = {
        "task_id": "task-1",
        "push_notification_config": {"url": "https://example.com/webhook"},
    }
    snapshot = copy.deepcopy(payload)
    TaskPushNotificationConfig.model_validate(payload)
    assert payload == snapshot


def test_validate_flat_v10_shape_still_works():
    config = TaskPushNotificationConfig(task_id="t", url="https://example.com", token="tok")
    assert config.push_notification_config.url == "https://example.com"


def test_validate_nested_model_instance():
    config = TaskPushNotificationConfig(
        task_id="t",
        push_notification_config=PushNotificationConfig(url="https://example.com"),
    )
    assert config.url == "https://example.com"
