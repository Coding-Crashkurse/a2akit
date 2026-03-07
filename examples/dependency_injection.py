"""Dependency injection example — all three DI patterns.

Demonstrates:
1. Lifecycle-managed dependency (HttpClient with startup/shutdown)
2. Plain value (AppConfig dataclass, string key)
3. Constructor injection (worker-specific system prompt)

Run:
    uvicorn examples.dependency_injection:app --reload
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from a2akit import A2AServer, AgentCardConfig, Dependency, TaskContext, Worker


class HttpClient(Dependency):
    """Simulated async HTTP client with lifecycle."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self._session: str | None = None  # placeholder for a real session

    async def startup(self) -> None:
        self._session = f"session-for-{self.base_url}"

    async def shutdown(self) -> None:
        self._session = None

    async def get(self, path: str) -> str:
        return f"[GET {self.base_url}{path} via {self._session}]"


@dataclass(frozen=True)
class AppConfig:
    model: str = "claude-sonnet"
    max_tokens: int = 1024


class MyWorker(Worker):
    def __init__(self, system_prompt: str = "You are helpful.") -> None:
        self.system_prompt = system_prompt  # constructor injection

    async def handle(self, ctx: TaskContext) -> None:
        # Shared infrastructure via DI:
        client = ctx.deps[HttpClient]
        config = ctx.deps[AppConfig]
        api_key = ctx.deps.get("api_key", "not-set")

        result = await client.get("/v1/data")
        lines = [
            f"Prompt: {self.system_prompt}",
            f"Model: {config.model}",
            f"API key: {api_key[:8]}...",
            f"HTTP result: {result}",
            f"User said: {ctx.user_text}",
        ]
        await ctx.complete("\n".join(lines))


server = A2AServer(
    worker=MyWorker(system_prompt="Analyze the request."),
    agent_card=AgentCardConfig(
        name="DI Demo Agent",
        description="Demonstrates dependency injection patterns.",
        version="0.1.0",
    ),
    dependencies={
        HttpClient: HttpClient(base_url="https://api.example.com"),
        AppConfig: AppConfig(model="claude-sonnet", max_tokens=2048),
        "api_key": os.environ.get("API_KEY", "sk-demo-key-12345"),
    },
)
app = server.as_fastapi_app()
