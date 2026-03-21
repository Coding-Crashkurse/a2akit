# Authenticated Extended Card Example

This example demonstrates how to serve a richer agent card to authenticated callers using the `extended_card_provider` callback.

## Server

```python title="examples/authenticated_card/server.py"
--8<-- "examples/authenticated_card/server.py"
```

The provider inspects the `Authorization` header and returns additional
skills for authenticated callers.

## Client

```python title="examples/authenticated_card/client.py"
--8<-- "examples/authenticated_card/client.py"
```

## Running

Start the server:

```bash
uvicorn examples.authenticated_card.server:app --reload
```

Run the client:

```bash
python -m examples.authenticated_card.client
```

Expected output:

```
Public card skills: 0
Extended card skills: 1
  - Echo: Echoes your input.
```

The public card has no skills (none declared on the base `AgentCardConfig`),
while the extended card returns the skills defined by the provider callback.
With a `Bearer` token, the provider would return an additional premium skill.
