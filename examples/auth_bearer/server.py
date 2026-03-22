"""Bearer Token Auth Server — validates JWT-like tokens.

Run:
    python examples/auth_bearer/server.py

Test:
    python examples/auth_bearer/client.py
"""

import uvicorn

from a2akit import (
    A2AServer,
    AgentCardConfig,
    BearerTokenMiddleware,
    TaskContext,
    Worker,
)

# Simple token verification (replace with real JWT validation)
VALID_TOKENS = {
    "secret-token-123": {"sub": "user1", "role": "admin"},
    "secret-token-456": {"sub": "user2", "role": "viewer"},
}


async def verify_token(token: str) -> dict | None:
    """Return claims dict if token is valid, None otherwise."""
    return VALID_TOKENS.get(token)


class GreetWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        claims = ctx.request_context.get("auth_claims", {})
        user = claims.get("sub", "unknown")
        await ctx.complete(f"Hello {user}! You said: {ctx.user_text}")


server = A2AServer(
    worker=GreetWorker(),
    agent_card=AgentCardConfig(
        name="Auth Echo",
        description="Bearer-token protected agent",
    ),
    middlewares=[BearerTokenMiddleware(verify=verify_token)],
)
app = server.as_fastapi_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
