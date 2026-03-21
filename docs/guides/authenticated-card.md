# Authenticated Extended Card

A2A v0.3.0 allows agents to serve a richer agent card to authenticated callers
(spec sections 5.5, 7.10, 9.1). The public card at `/.well-known/agent-card.json`
advertises basic capabilities, while the authenticated extended card can expose
additional skills, modes, or metadata based on who is calling.

## How It Works

1. You provide an `extended_card_provider` callback to `A2AServer`.
2. The callback receives the raw `Request` and returns an `AgentCardConfig`.
3. a2akit automatically sets `supportsAuthenticatedExtendedCard: true` on the public card.
4. Clients call `GET /v1/card` (REST) or `agent/getAuthenticatedExtendedCard` (JSON-RPC) to fetch the extended card.

## Server Setup

```python
from fastapi import Request
from a2akit import A2AServer, AgentCardConfig, SkillConfig, TaskContext, Worker


class MyWorker(Worker):
    async def handle(self, ctx: TaskContext) -> None:
        await ctx.complete(f"Echo: {ctx.user_text}")


async def provide_extended_card(request: Request) -> AgentCardConfig:
    """Return a richer card for authenticated callers."""
    auth = request.headers.get("Authorization", "")
    skills = [
        SkillConfig(id="echo", name="Echo", description="Echoes input.", tags=[]),
    ]
    if auth.startswith("Bearer "):
        # Authenticated callers see premium skills
        skills.append(
            SkillConfig(
                id="premium-echo",
                name="Premium Echo",
                description="Premium echo with formatting.",
                tags=["premium"],
            )
        )

    return AgentCardConfig(
        name="My Agent",
        description="Agent with authenticated extended card.",
        version="1.0.0",
        protocol="http+json",
        skills=skills,
    )


server = A2AServer(
    worker=MyWorker(),
    agent_card=AgentCardConfig(
        name="My Agent",
        description="Agent with authenticated extended card.",
        version="1.0.0",
        protocol="http+json",
    ),
    extended_card_provider=provide_extended_card,
)
app = server.as_fastapi_app()
```

## Client Usage

```python
from a2akit import A2AClient

async with A2AClient("http://localhost:8000") as client:
    # Public card
    print(f"Public skills: {len(client.agent_card.skills)}")
    print(f"Supports extended card: {client.agent_card.supports_authenticated_extended_card}")

    # Extended card (with auth headers, the provider can return more skills)
    extended = await client.get_extended_card()
    print(f"Extended skills: {len(extended.skills)}")
```

## Transports

| Transport | Endpoint | Error (not configured) |
|-----------|----------|----------------------|
| HTTP+JSON | `GET /v1/card` | 404, code `-32007` |
| JSON-RPC | `agent/getAuthenticatedExtendedCard` | Error code `-32007` |

## When Not Configured

If no `extended_card_provider` is passed to `A2AServer`:

- `supportsAuthenticatedExtendedCard` is `false` (or absent) on the public card
- Requests to the extended card endpoint return error code `-32007`
