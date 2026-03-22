"""Multi-Transport Server — serves both JSON-RPC and REST simultaneously.

Run:
    python examples/multi_transport/server.py

Test:
    # JSON-RPC (preferred)
    python examples/multi_transport/client.py

    # REST
    curl -X POST http://localhost:8000/v1/message:send \
         -H "Content-Type: application/json" \
         -d '{"message": {"role": "user", "parts": [{"kind": "text", "text": "Hello"}], "messageId": "1"}}'
"""

import uvicorn

from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig, TaskContext, Worker


class EchoWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


server = A2AServer(
    worker=EchoWorker(),
    agent_card=AgentCardConfig(
        name="Multi-Transport Echo",
        description="Agent serving both JSON-RPC and REST",
        protocol="jsonrpc",
        capabilities=CapabilitiesConfig(streaming=True),
    ),
    additional_protocols=["HTTP"],
)
app = server.as_fastapi_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
