"""Streaming worker — emits word-by-word artifacts with progress updates."""

import asyncio

from a2akit import A2AServer, AgentCardConfig, TaskContext, Worker


class StreamingWorker(Worker):
    """Streams the user's input back word by word."""

    async def handle(self, ctx: TaskContext) -> None:
        """Split user text into words and emit each as a streaming artifact chunk."""
        words = ctx.user_text.split()
        await ctx.send_status(f"Streaming {len(words)} words...")

        for i, word in enumerate(words):
            is_last = i == len(words) - 1
            await ctx.emit_text_artifact(
                text=word + ("" if is_last else " "),
                artifact_id="stream",
                append=(i > 0),
                last_chunk=is_last,
            )
            await asyncio.sleep(0.1)

        await ctx.complete()


server = A2AServer(
    worker=StreamingWorker(),
    agent_card=AgentCardConfig(
        name="Streamer", description="Word-by-word streaming", version="0.1.0"
    ),
)
app = server.as_fastapi_app()
