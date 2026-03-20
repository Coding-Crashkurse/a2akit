"""Extension client example — discovers and inspects agent extensions.

Run the server first:
    uvicorn examples.extensions:app --reload

Then run this client:
    python -m examples.extensions_client
"""

import asyncio

from a2akit import A2AClient


async def main() -> None:
    async with A2AClient("http://localhost:8000") as client:
        card = client.agent_card
        print(f"Agent: {card.name}")
        print(f"Description: {card.description}")

        if card.capabilities and card.capabilities.extensions:
            print(f"\nDeclared extensions ({len(card.capabilities.extensions)}):")
            for ext in card.capabilities.extensions:
                print(f"  - {ext.uri}")
                if ext.description:
                    print(f"    Description: {ext.description}")
                if ext.required:
                    print("    Required: yes")
                if ext.params:
                    print(f"    Params: {ext.params}")
        else:
            print("\nNo extensions declared.")

        # Send a normal message — extensions are declarative, they don't
        # change the message flow.
        result = await client.send("Hello from extensions client!")
        print(f"\nResponse: {result.text}")


if __name__ == "__main__":
    asyncio.run(main())
