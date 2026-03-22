"""Subscribe Replay Client — demonstrates Last-Event-ID resume.

Run:
    python examples/subscribe_replay/client.py
"""

import asyncio

from a2akit import A2AClient


async def main():
    async with A2AClient("http://localhost:8000") as client:
        # Start a streaming task
        last_event_id = None
        task_id = None

        async for event in client.stream("Generate chunks"):
            print(f"Event: {event.kind} | text={event.text}")
            task_id = event.task_id
            if event.event_id:
                last_event_id = event.event_id
            # Simulate disconnect after 2 events
            if last_event_id and int(last_event_id) >= 2:
                print(f"\n--- Simulating disconnect at event {last_event_id} ---\n")
                break

        # Resubscribe with Last-Event-ID to get remaining events
        if task_id and last_event_id:
            print(f"Resubscribing from event {last_event_id}...")
            async for event in client.subscribe(task_id, last_event_id=last_event_id):
                print(f"Replayed: {event.kind} | text={event.text}")


if __name__ == "__main__":
    asyncio.run(main())
