"""Subscribe Replay Server — demonstrates Last-Event-ID SSE replay.

Run:
    python examples/subscribe_replay/server.py
"""

import asyncio

import uvicorn

from a2akit import A2AServer, AgentCardConfig, CapabilitiesConfig, TaskContext, Worker


class StreamingWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        for i in range(5):
            await ctx.emit_text_artifact(
                f"Chunk {i + 1}",
                artifact_id="stream",
                append=i > 0,
                last_chunk=i == 4,
            )
            await asyncio.sleep(0.3)
        await ctx.complete()


server = A2AServer(
    worker=StreamingWorker(),
    agent_card=AgentCardConfig(
        name="Replay Demo",
        description="Agent demonstrating SSE Last-Event-ID replay",
        capabilities=CapabilitiesConfig(streaming=True),
    ),
)
app = server.as_fastapi_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
