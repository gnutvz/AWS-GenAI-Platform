"""The agent itself — one definition, three deployment targets.

Nothing in this module knows whether it is running in Lambda, in AgentCore
Runtime, or on a laptop. That is the point: `build_agent()` is what gets promoted
from reference to production, not rewritten for it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from strands import Agent
from strands.session import S3SessionManager

from aiplat import build_model, prompts, settings, setup_tracing, trace_attributes
from aiplat.knowledge import Filters, make_search_tool

logger = logging.getLogger(__name__)

# Prompts belong to this workload, not to the platform — they instruct one use
# case. The loader that versions them is shared; the text is not.
#
# A directory per prompt, selected by PROMPT_NAME, so tenants that need different
# instructions get them without a fork. `system` is the one every tenant shared
# before that was possible.
PROMPTS_DIR = Path(__file__).parent / "prompts"


def system_prompts(name: str | None = None) -> Path:
    """Where this deployment's prompt versions live."""
    return PROMPTS_DIR / (name or settings().prompt_name)


# Kept for callers that want the default prompt without reading settings.
SYSTEM_PROMPTS = PROMPTS_DIR / "system"


def build_agent(
    session_id: str | None = None, retrieval_filters: Filters | None = None
) -> Agent:
    """Construct an agent wired to the platform's model, tools, tracing and state.

    Args:
        session_id: Conversation to resume. None means a stateless one-shot call.
        retrieval_filters: Metadata every retrieved passage must match. Baked
            into the tool, so the model cannot widen them. Nothing supplies these
            yet — end-user identity does not exist here, and this is the seam it
            will arrive through.

    The tenant is not a parameter: it is fixed by the deployment (TENANT in the
    environment). Letting a caller pass it would let a caller claim to be someone
    else on every trace and cost report.
    """
    setup_tracing()
    cfg = settings()
    prompt = prompts.load(system_prompts(cfg.prompt_name), cfg.prompt_version)

    tools = []
    if cfg.retrieval_enabled:
        tools.append(make_search_tool(retrieval_filters))
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
        system_prompt=prompt.text,
        session_manager=session_manager,
        # The prompt version rides along on every span. A trace that records what
        # the model said but not what it was told cannot explain a regression.
        trace_attributes=trace_attributes(
            tenant=cfg.tenant, session_id=session_id or "", prompt=prompt.label
        ),
        # Direct tool calls in evals should not pollute conversation history.
        record_direct_tool_call=False,
    )


async def ask(prompt: str, session_id: str | None = None) -> dict:
    """One turn. Returns the answer plus the numbers worth logging."""
    cfg = settings()
    agent = build_agent(session_id=session_id)
    result = await agent.invoke_async(prompt)

    usage = getattr(result.metrics, "accumulated_usage", {}) or {}
    return {
        "answer": str(result),
        "stop_reason": result.stop_reason,
        "session_id": session_id,
        "tenant": cfg.tenant,
        # Which prompt produced this. Without it a client reporting a bad answer
        # is describing behaviour nobody can reproduce.
        "prompt": prompts.load(system_prompts(cfg.prompt_name), cfg.prompt_version).label,
        "usage": {
            "input_tokens": usage.get("inputTokens"),
            "output_tokens": usage.get("outputTokens"),
            "total_tokens": usage.get("totalTokens"),
        },
    }
