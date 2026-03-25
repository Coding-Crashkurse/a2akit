"""CLI client for the research pipeline — streams results in the terminal.

Usage:
    python client.py "AI agent frameworks"
    python client.py "Electric vehicle market in Europe"
"""

from __future__ import annotations

import asyncio
import sys

from a2akit import A2AClient


async def main(query: str) -> None:
    async with A2AClient("http://localhost:8000") as client:
        card = client.agent_card
        print(f"Connected to: {card.name} v{card.version}")
        print(f"  Streaming: {card.capabilities.streaming}")
        print(f"  Skills: {', '.join(s.name for s in card.skills)}")
        print(f"  Query: {query}")
        print("-" * 60)

        current_artifact: str | None = None

        async for event in client.stream(query):
            if event.kind == "status" and event.text:
                print(f"  > {event.text}")

            elif event.kind == "artifact" and event.text:
                if event.artifact_id != current_artifact:
                    current_artifact = event.artifact_id
                    print(f"\n  [{current_artifact}]")
                print(f"    {event.text}", end="")

            elif event.kind == "completed":
                if event.text:
                    print(f"\n{'=' * 60}")
                    print(event.text)
                    print(f"{'=' * 60}")

            elif event.kind == "failed":
                print(f"\n  FAILED: {event.text}")

            elif event.kind == "canceled":
                print("\n  Task was cancelled")

        print("\nDone.")


if __name__ == "__main__":
    query = " ".join(sys.argv[1:]) or "AI agent frameworks"
    asyncio.run(main(query))
