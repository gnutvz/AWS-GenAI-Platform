"""The agent itself — one definition, three deployment targets.

Nothing in this module knows whether it is running in Lambda, in AgentCore
Runtime, or on a laptop. That is the point: `build_agent()` is what gets promoted
from reference to production, not rewritten for it.
"""

from __future__ import annotations

import logging

from strands import Agent
from strands.session import S3SessionManager

from aiplat import build_model, settings, setup_tracing, trace_attributes
from aiplat.knowledge import search_knowledge_base

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an internal assistant for an enterprise team.

Rules you do not break:
- Answer from retrieved passages, not from memory. If you did not retrieve it, say so.
- Cite the source of every factual claim using the [n] markers from search results.
- If the knowledge base returns nothing relevant, say you do not know and suggest
  what the user could search for instead. Never fill the gap with a plausible guess.
- Keep answers short. Length is not helpfulness.
"""


def build_agent(session_id: str | None = None, tenant: str = "default") -> Agent:
    """Construct an agent wired to the platform's model, tools, tracing and state.

    Args:
        session_id: Conversation to resume. None means a stateless one-shot call.
        tenant: Stamped on traces so cost and latency can be sliced per team.
    """
    setup_tracing()
    cfg = settings()

    tools = []
    if cfg.retrieval_enabled:
        tools.append(search_knowledge_base)
    else:
        logger.warning("KNOWLEDGE_BASE_ID unset; agent is running without retrieval")

    # State in S3 keeps the compute stateless, so the same agent survives a Lambda
    # cold start, an AgentCore session, and a local run without changing shape.
    session_manager = None
    if session_id and cfg.session_bucket:
        session_manager = S3SessionManager(
            session_id=session_id,
            bucket=cfg.session_bucket,
            prefix="sessions/",
            region_name=cfg.region,
        )
    elif session_id:
        logger.warning("session_id given but SESSION_BUCKET unset; conversation will not persist")

    return Agent(
        model=build_model(),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        session_manager=session_manager,
        trace_attributes=trace_attributes(tenant=tenant, session_id=session_id or ""),
        # Direct tool calls in evals should not pollute conversation history.
        record_direct_tool_call=False,
    )


async def ask(prompt: str, session_id: str | None = None, tenant: str = "default") -> dict:
    """One turn. Returns the answer plus the numbers worth logging."""
    agent = build_agent(session_id=session_id, tenant=tenant)
    result = await agent.invoke_async(prompt)

    usage = getattr(result.metrics, "accumulated_usage", {}) or {}
    return {
        "answer": str(result),
        "stop_reason": result.stop_reason,
        "session_id": session_id,
        "usage": {
            "input_tokens": usage.get("inputTokens"),
            "output_tokens": usage.get("outputTokens"),
            "total_tokens": usage.get("totalTokens"),
        },
    }
