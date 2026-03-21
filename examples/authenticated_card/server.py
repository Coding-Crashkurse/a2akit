"""Authenticated extended card example.

Run:
    uvicorn examples.authenticated_card.server:app --reload
"""

from fastapi import Request

from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig, SkillConfig, TaskContext, Worker


class EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


async def provide_extended_card(request: Request) -> AgentCardConfig:
    """Return a richer card for authenticated callers.

    In production, inspect request.headers["Authorization"] to verify
    the caller and tailor the returned card (e.g., expose premium skills).
    """
    auth = request.headers.get("Authorization", "")
    skills = [
        SkillConfig(
            id="echo",
            name="Echo",
            description="Echoes your input.",
            tags=["demo"],
        ),
    ]
    if auth.startswith("Bearer "):
        skills.append(
            SkillConfig(
                id="premium-echo",
                name="Premium Echo",
                description="Premium echo with formatting.",
                tags=["demo", "premium"],
            )
        )

    return AgentCardConfig(
        name="Extended Card Demo",
        description="Agent with authenticated extended card.",
        version="1.0.0",
        protocol="http+json",
        capabilities=CapabilitiesConfig(),
        skills=skills,
    )


server = A2AServer(
    worker=EchoWorker(),
    agent_card=AgentCardConfig(
        name="Extended Card Demo",
        description="Agent with authenticated extended card.",
        version="1.0.0",
        protocol="http+json",
    ),
    extended_card_provider=provide_extended_card,
)
app = server.as_fastapi_app(debug=True)
