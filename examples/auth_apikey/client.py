"""API Key Auth Client — sends requests with X-API-Key header.

Run:
    python examples/auth_apikey/client.py
"""

import asyncio

from a2akit import A2AClient


async def main():
    async with A2AClient(
        "http://localhost:8000",
        headers={"X-API-Key": "sk-abc123"},
    ) as client:
        result = await client.send("Hello!")
        print(f"Response: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
