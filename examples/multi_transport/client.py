"""Multi-Transport Client — connects to the multi-transport server.

Run:
    python examples/multi_transport/client.py
"""

import asyncio

from a2akit import A2AClient


async def main():
    # Default: auto-detects preferred protocol from agent card
    async with A2AClient("http://localhost:8000") as client:
        print(f"Connected via: {client.protocol}")
        result = await client.send("Hello from auto-detected transport!")
        print(f"Response: {result.text}")

    # Force REST protocol
    async with A2AClient("http://localhost:8000", protocol="http+json") as client:
        print(f"Connected via: {client.protocol}")
        result = await client.send("Hello from REST!")
        print(f"Response: {result.text}")

    # Force JSON-RPC protocol
    async with A2AClient("http://localhost:8000", protocol="jsonrpc") as client:
        print(f"Connected via: {client.protocol}")
        result = await client.send("Hello from JSON-RPC!")
        print(f"Response: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
