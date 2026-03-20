"""Semantic convention constants for a2akit OpenTelemetry instrumentation."""

# Tracer / Meter identity
TRACER_NAME = "a2akit"
METER_NAME = "a2akit"
TRACER_VERSION = "0.0.13"

# --- Span Names ---
SPAN_TASK_PROCESS = "a2akit.task.process"
SPAN_HTTP_REQUEST = "a2akit.http.request"
SPAN_CLIENT_SEND = "a2akit.client.send"
SPAN_CLIENT_STREAM = "a2akit.client.stream"
SPAN_CLIENT_GET_TASK = "a2akit.client.get_task"
SPAN_CLIENT_LIST_TASKS = "a2akit.client.list_tasks"
SPAN_CLIENT_CANCEL = "a2akit.client.cancel"
SPAN_CLIENT_SUBSCRIBE = "a2akit.client.subscribe"
SPAN_CLIENT_CONNECT = "a2akit.client.connect"

# --- Attribute Keys ---
ATTR_TASK_ID = "a2akit.task.id"
ATTR_CONTEXT_ID = "a2akit.context.id"
ATTR_MESSAGE_ID = "a2akit.message.id"
ATTR_TASK_STATE = "a2akit.task.state"
ATTR_AGENT_NAME = "a2akit.agent.name"
ATTR_PROTOCOL = "a2akit.protocol"
ATTR_IS_NEW_TASK = "a2akit.task.is_new"
ATTR_WORKER_CLASS = "a2akit.worker.class"
ATTR_METHOD = "a2akit.method"
ATTR_IS_STREAMING = "a2akit.streaming"
ATTR_ARTIFACT_COUNT = "a2akit.artifact.count"
ATTR_ERROR_TYPE = "a2akit.error.type"

# --- Span Event Names ---
EVENT_STATE_TRANSITION = "state_transition"
EVENT_ARTIFACT_EMITTED = "artifact_emitted"
EVENT_CANCEL_REQUESTED = "cancel_requested"

# --- Metric Names ---
METRIC_TASK_DURATION = "a2akit.task.duration"
METRIC_TASK_ACTIVE = "a2akit.task.active"
METRIC_TASK_TOTAL = "a2akit.task.total"
METRIC_TASK_ERRORS = "a2akit.task.errors"
METRIC_CLIENT_REQUEST_DURATION = "a2akit.client.request.duration"
