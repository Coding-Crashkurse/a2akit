"""Simple echo worker — returns the user's input back."""

from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig, TaskContext, Worker


class EchoWorker(Worker):
    """Echoes the user's message back as-is."""

    async def handle(self, ctx: TaskContext) -> None:
        """Echo the user text back."""
        if ctx.user_text == "fail":
            await ctx.fail(f"Echo: {ctx.user_text}")
        await ctx.complete(f"Echo: {ctx.user_text}")


server = A2AServer(
    worker=EchoWorker(),
    agent_card=AgentCardConfig(
        name="Echo",
        description="Echoes your input back.",
        version="0.1.0",
        protocol="http+json",
        capabilities=CapabilitiesConfig(state_transition_history=True),
    ),
)
app = server.as_fastapi_app(debug=True)
