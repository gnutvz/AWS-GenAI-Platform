"""AgentCore Runtime entrypoint — the production path.

Not deployed by default. This file exists so the promotion from Lambda to a managed
agent runtime is a deployment change rather than a redesign: identical
`build_agent()`, identical tools, identical tracing.

Take this path when you need streaming responses, sessions longer than 15 minutes,
or AgentCore's managed identity and browser/code-interpreter tools.

    pip install 'aiplat[agentcore]'
    python -m services.agent.agentcore_app
"""

from __future__ import annotations

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from services.agent.agent import build_agent

app = BedrockAgentCoreApp()


@app.entrypoint
async def invoke(payload: dict):
    """Stream tokens back as they are produced."""
    prompt = payload.get("prompt", "")
    if not prompt:
        yield "Field 'prompt' is required."
        return

    agent = build_agent(
        session_id=payload.get("session_id"),
        tenant=payload.get("tenant", "default"),
    )

    async for event in agent.stream_async(prompt):
        if chunk := event.get("data"):
            yield chunk


if __name__ == "__main__":
    app.run()
