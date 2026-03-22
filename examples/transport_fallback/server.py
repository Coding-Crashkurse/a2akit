"""Transport Fallback Server — advertises JSON-RPC + REST.

This example demonstrates the client's transport fallback behavior.
The server offers both transports; see client.py for fallback logic.

Run:
    python examples/transport_fallback/server.py
"""

import uvicorn

from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig, TaskContext, Worker


class EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


server = A2AServer(
    worker=EchoWorker(),
    agent_card=AgentCardConfig(
        name="Fallback Demo",
        description="Agent with multiple transports for fallback testing",
        protocol="jsonrpc",
        capabilities=CapabilitiesConfig(streaming=True),
    ),
    additional_protocols=["HTTP"],
)
app = server.as_fastapi_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
