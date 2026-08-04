"""Lambda entrypoint — the default deployment target.

Chosen over a container because idle cost is genuinely zero and the agent is
already stateless. The ceiling is real though: 15-minute timeout and no streaming
to the client through a plain function URL. When either starts to bite, the same
`build_agent()` moves to AgentCore Runtime (see agentcore_app.py) without touching
agent logic.
"""

from __future__ import annotations

import asyncio
import json
import logging

from services.agent.agent import ask

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)


def handler(event: dict, context: object) -> dict:
    try:
        body = _parse_body(event)
        prompt = body.get("prompt")
        if not prompt:
            return _response(400, {"error": "Field 'prompt' is required"})

        result = asyncio.run(
            ask(
                prompt=prompt,
                session_id=body.get("session_id"),
                tenant=body.get("tenant", "default"),
            )
        )
        return _response(200, result)

    except Exception as exc:
        # Log the detail, return a generic message: stack traces are not a public API.
        logger.exception("Agent invocation failed")
        return _response(500, {"error": "Agent invocation failed", "type": type(exc).__name__})


def _parse_body(event: dict) -> dict:
    """Accept both API Gateway proxy events and direct invocation payloads."""
    if "body" not in event:
        return event
    body = event["body"]
    return json.loads(body) if isinstance(body, str) else (body or {})


def _response(status: int, payload: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload, ensure_ascii=False),
    }
