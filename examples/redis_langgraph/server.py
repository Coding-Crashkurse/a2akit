"""Production LangGraph agent — Redis broker/event bus/cancel, Postgres storage.

Demonstrates:
  1. LangGraph StateGraph with 3 nodes and custom stream events
  2. Redis as broker, event bus, AND cancel registry
  3. PostgreSQL as durable task storage
  4. Cooperative cancellation inside graph nodes
  5. Streaming artifact chunks from LangGraph -> A2A SSE
  6. Built-in debug UI at /chat (enabled via debug=True)

Start via Docker:
    docker compose up --build

Start locally (requires running Redis + Postgres):
    REDIS_URL=redis://localhost:6379/0 \
    DATABASE_URL=postgresql+asyncpg://a2akit:a2akit@localhost/a2akit \
    python server.py
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from a2akit import (
    A2AServer,
    AgentCardConfig,
    CapabilitiesConfig,
    SkillConfig,
    TaskContext,
    Worker,
)
from a2akit.hooks import LifecycleHooks

# ---------------------------------------------------------------------------
# Configuration from environment
# ---------------------------------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+asyncpg://a2akit:a2akit@localhost/a2akit",
)

# ---------------------------------------------------------------------------
# LangGraph pipeline — research -> analyze -> summarize
# ---------------------------------------------------------------------------

SIMULATED_SOURCES = [
    {
        "title": "Market Report Q1",
        "relevance": 0.95,
        "content": "Revenue grew 12% YoY driven by new product lines and international expansion.",
    },
    {
        "title": "Competitor Analysis",
        "relevance": 0.87,
        "content": "Main competitor launched a budget tier; pricing pressure expected in H2.",
    },
    {
        "title": "Customer Survey 2024",
        "relevance": 0.82,
        "content": "NPS improved to 72. Top request: better API documentation.",
    },
    {
        "title": "Industry Trends",
        "relevance": 0.78,
        "content": "AI adoption accelerated across verticals. Agent frameworks gaining traction.",
    },
    {
        "title": "Internal Memo: Strategy",
        "relevance": 0.91,
        "content": "Focus areas for H2: platform reliability, developer experience, partnerships.",
    },
]


class ResearchState(TypedDict):
    query: str
    sources: list[dict]
    analysis: str
    summary: str


async def research_node(state: ResearchState) -> dict:
    """Simulate searching multiple sources — emits progress via custom stream events."""
    writer = get_stream_writer()
    sources_found: list[dict] = []

    writer({"type": "status", "message": f"Searching for: {state['query']}", "phase": "research"})

    for i, source in enumerate(SIMULATED_SOURCES, 1):
        await asyncio.sleep(0.4)
        sources_found.append(source)
        writer(
            {
                "type": "source_found",
                "index": i,
                "total": len(SIMULATED_SOURCES),
                "title": source["title"],
                "relevance": source["relevance"],
                "phase": "research",
            }
        )

    writer(
        {
            "type": "status",
            "message": f"Found {len(sources_found)} relevant sources",
            "phase": "research",
        }
    )
    return {"sources": sources_found}


async def analyze_node(state: ResearchState) -> dict:
    """Analyze collected sources — streams section-by-section."""
    writer = get_stream_writer()
    writer({"type": "status", "message": "Analyzing sources...", "phase": "analysis"})

    sections = []
    for i, source in enumerate(state["sources"]):
        await asyncio.sleep(0.3)
        section = (
            f"### {source['title']} (relevance: {source['relevance']:.0%})\n{source['content']}\n"
        )
        sections.append(section)
        writer(
            {
                "type": "analysis_chunk",
                "section": section,
                "index": i + 1,
                "total": len(state["sources"]),
                "phase": "analysis",
            }
        )

    writer({"type": "status", "message": "Analysis complete", "phase": "analysis"})
    return {"analysis": "\n".join(sections)}


async def summarize_node(state: ResearchState) -> dict:
    """Produce final summary from analysis."""
    writer = get_stream_writer()
    writer({"type": "status", "message": "Generating executive summary...", "phase": "summary"})

    await asyncio.sleep(0.5)

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    summary = (
        f"# Research Summary\n\n"
        f"**Query:** {state['query']}\n"
        f"**Generated:** {now}\n"
        f"**Sources analyzed:** {len(state['sources'])}\n\n"
        f"## Key Findings\n\n"
        f"{state['analysis']}\n\n"
        f"## Conclusion\n\n"
        f"Based on {len(state['sources'])} sources, the research indicates "
        f"positive momentum across all tracked dimensions.\n"
    )

    writer({"type": "summary_complete", "summary": summary, "phase": "summary"})
    return {"summary": summary}


# Build the graph
graph = (
    StateGraph(ResearchState)
    .add_node("research", research_node)
    .add_node("analyze", analyze_node)
    .add_node("summarize", summarize_node)
    .add_edge(START, "research")
    .add_edge("research", "analyze")
    .add_edge("analyze", "summarize")
    .add_edge("summarize", END)
    .compile()
)

# ---------------------------------------------------------------------------
# A2A Worker — bridges LangGraph stream events to A2A SSE
# ---------------------------------------------------------------------------


class ResearchWorker(Worker):
    """Maps LangGraph custom stream events to A2A streaming primitives."""

    async def handle(self, ctx: TaskContext) -> None:
        query = ctx.user_text
        if not query.strip():
            await ctx.fail("Please provide a research query.")
            return

        await ctx.send_status(f"Starting research pipeline for: {query}")

        src_idx = 0
        ana_idx = 0

        async for _mode, chunk in graph.astream(
            {"query": query, "sources": [], "analysis": "", "summary": ""},
            stream_mode=["custom"],
        ):
            # Cooperative cancellation
            if ctx.is_cancelled:
                await ctx.send_status("Cancelled by user")
                return

            evt = chunk.get("type", "")
            phase = chunk.get("phase", "")

            if evt == "status":
                await ctx.send_status(f"[{phase}] {chunk['message']}")

            elif evt == "source_found":
                line = (
                    f"[{chunk['index']}/{chunk['total']}] "
                    f"{chunk['title']} — relevance {chunk['relevance']:.0%}\n"
                )
                await ctx.emit_text_artifact(
                    text=line,
                    artifact_id="sources",
                    append=(src_idx > 0),
                    last_chunk=(chunk["index"] == chunk["total"]),
                )
                src_idx += 1

            elif evt == "analysis_chunk":
                await ctx.emit_text_artifact(
                    text=chunk["section"],
                    artifact_id="analysis",
                    append=(ana_idx > 0),
                    last_chunk=(chunk["index"] == chunk["total"]),
                )
                ana_idx += 1

            elif evt == "summary_complete":
                await ctx.complete(chunk["summary"])
                return

        if not ctx.turn_ended:
            await ctx.fail("Pipeline finished without producing a summary")


# ---------------------------------------------------------------------------
# Lifecycle hooks — observability
# ---------------------------------------------------------------------------


async def on_working(task_id: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] > Task {task_id[:8]}... started")


async def on_terminal(task_id, state, message) -> None:
    msg_text = ""
    if message and message.parts:
        for part in message.parts:
            root = getattr(part, "root", part)
            if hasattr(root, "text") and root.text:
                msg_text = root.text[:80]
                break
    print(
        f"[{datetime.now().strftime('%H:%M:%S')}] # Task {task_id[:8]}... -> {state.value}"
        f"{' -- ' + msg_text if msg_text else ''}"
    )


hooks = LifecycleHooks(on_working=on_working, on_terminal=on_terminal)

# ---------------------------------------------------------------------------
# Server assembly
# ---------------------------------------------------------------------------

server = A2AServer(
    worker=ResearchWorker(),
    agent_card=AgentCardConfig(
        name="Research Pipeline Agent",
        description=(
            "Multi-stage research agent powered by LangGraph. "
            "Streams progress and partial results in real time."
        ),
        version="1.0.0",
        capabilities=CapabilitiesConfig(
            streaming=True,
            push_notifications=True,
            state_transition_history=True,
        ),
        skills=[
            SkillConfig(
                id="research",
                name="Research & Summarize",
                description="Research a topic and produce an executive summary.",
                tags=["research", "analysis", "summarization"],
                examples=[
                    "Research the current state of AI agent frameworks",
                    "Analyze market trends for electric vehicles in Europe",
                ],
                input_modes=["text/plain"],
                output_modes=["text/plain", "text/markdown"],
            ),
        ],
    ),
    broker=REDIS_URL,
    event_bus=REDIS_URL,
    storage=DATABASE_URL,
    hooks=hooks,
    max_concurrent_tasks=10,
    blocking_timeout_s=120,
)

# -- FastAPI app with built-in debug UI at /chat --

app = server.as_fastapi_app(debug=True)

if __name__ == "__main__":
    import uvicorn

    workers = int(os.environ.get("UVICORN_WORKERS", "1"))
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        workers=workers,
        log_level="info",
    )
