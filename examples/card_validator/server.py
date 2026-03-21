"""Echo server with a JWS signature on its agent card.

Run:
    uvicorn examples.card_validator.server:app --reload
"""

from a2akit import (
    A2AServer,
    AgentCardConfig,
    CapabilitiesConfig,
    SignatureConfig,
    TaskContext,
    Worker,
)


class EchoWorker(Worker):
    """Echoes the user's message back."""

    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


server = A2AServer(
    worker=EchoWorker(),
    agent_card=AgentCardConfig(
        name="Signed Echo Agent",
        description="An echo agent whose card carries a JWS signature.",
        version="0.1.0",
        protocol="http+json",
        capabilities=CapabilitiesConfig(),
        signatures=[
            SignatureConfig(
                protected="eyJhbGciOiJFUzI1NiJ9",
                signature="placeholder-sig-for-demo",
            ),
        ],
    ),
)
app = server.as_fastapi_app(debug=True)
