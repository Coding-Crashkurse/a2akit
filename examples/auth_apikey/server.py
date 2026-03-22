"""API Key Auth Server — validates keys from X-API-Key header.

Run:
    python examples/auth_apikey/server.py

Test:
    python examples/auth_apikey/client.py
"""

import uvicorn

from a2akit import A2AServer, AgentCardConfig, ApiKeyMiddleware, TaskContext, Worker

VALID_KEYS = {"sk-abc123", "sk-def456"}


class EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        key = ctx.request_context.get("api_key", "?")
        await ctx.complete(f"Authenticated with key {key[:8]}...: {ctx.user_text}")


server = A2AServer(
    worker=EchoWorker(),
    agent_card=AgentCardConfig(
        name="API Key Echo",
        description="API-key protected agent",
    ),
    middlewares=[ApiKeyMiddleware(valid_keys=VALID_KEYS)],
)
app = server.as_fastapi_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
