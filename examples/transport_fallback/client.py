"""Transport Fallback Client — demonstrates automatic fallback.

The client tries the preferred transport first; if it fails a health
check, it falls back to additionalInterfaces from the agent card.

Run:
    python examples/transport_fallback/client.py
"""

import asyncio

from a2akit import A2AClient


async def main():
    # Client auto-discovers and falls back if needed
    async with A2AClient("http://localhost:8000") as client:
        print(f"Connected via: {client.protocol}")
        result = await client.send("Testing fallback!")
        print(f"Response: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
