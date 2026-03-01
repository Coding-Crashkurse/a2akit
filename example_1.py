"""Simple echo worker — returns the user's input back."""

from a2akit import A2AServer, AgentCardConfig, TaskContext, Worker


class EchoWorker(Worker):
    """Echoes the user's message back as-is."""

    async def handle(self, ctx: TaskContext) -> None:
        """Echo the user text back."""
        await ctx.complete(f"Echo: {ctx.user_text}")


server = A2AServer(
    worker=EchoWorker(),
    agent_card=AgentCardConfig(
        name="Echo", description="Echoes input", version="0.1.0"
    ),
)
app = server.as_fastapi_app()
