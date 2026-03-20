"""Extension declaration example — shows how to declare protocol extensions.

Run:
    uvicorn examples.extensions:app --reload
"""

from a2akit import (
    A2AServer,
    AgentCardConfig,
    CapabilitiesConfig,
    ExtensionConfig,
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
        name="Extension Demo",
        description="Agent that declares protocol extensions.",
        version="0.1.0",
        protocol="http+json",
        capabilities=CapabilitiesConfig(
            extensions=[
                ExtensionConfig(
                    uri="urn:example:custom-logging",
                    description="Custom structured logging extension",
                    params={"log_level": "debug", "format": "json"},
                ),
                ExtensionConfig(
                    uri="urn:example:rate-limiting",
                    description="Rate limiting metadata",
                    required=False,
                    params={"max_requests_per_minute": 60},
                ),
            ],
        ),
    ),
)
app = server.as_fastapi_app(debug=True)
