"""Bearer Token Auth Client — sends requests with Authorization header.

Run:
    python examples/auth_bearer/client.py
"""

import asyncio

from a2akit import A2AClient


async def main():
    # Authenticated request
    async with A2AClient(
        "http://localhost:8000",
        headers={"Authorization": "Bearer secret-token-123"},
    ) as client:
        result = await client.send("Hi there!")
        print(f"Authenticated: {result.text}")

    # Unauthenticated request → 401
    try:
        async with A2AClient("http://localhost:8000") as client:
            await client.send("Hi there!")
    except Exception as e:
        print(f"No token: {e}")


if __name__ == "__main__":
    asyncio.run(main())
