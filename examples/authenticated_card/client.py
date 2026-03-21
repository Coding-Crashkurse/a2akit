"""Client example -- fetch authenticated extended card.

Start the server first:
    uvicorn examples.authenticated_card.server:app

Then run this client:
    python -m examples.authenticated_card.client
"""

import asyncio

from a2akit import A2AClient


async def main():
    async with A2AClient("http://localhost:8000") as client:
        print(f"Public card skills: {len(client.agent_card.skills)}")

        extended = await client.get_extended_card()
        print(f"Extended card skills: {len(extended.skills)}")
        for skill in extended.skills:
            print(f"  - {skill.name}: {skill.description}")


if __name__ == "__main__":
    asyncio.run(main())
