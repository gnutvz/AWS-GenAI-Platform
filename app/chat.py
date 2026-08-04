"""Chat UI.

    chainlit run app/chat.py

Runs locally against whatever `.env` points at — no extra AWS infrastructure.
The same file also deploys to Slack, Teams, or an embedded widget; Chainlit
treats those as deployment targets rather than rewrites, which matters because
the people who would use this are already sitting in Teams all day.

The design choice worth stating: retrieval is shown, not hidden. Every search
appears as an expandable step listing the passages and where they came from, and
a refusal is rendered as a first-class outcome rather than an error. A demo where
the answer simply materialises looks like every other chatbot; watching the agent
look something up, cite it, and decline when it has nothing is the part that is
actually different.

Imports `build_agent` rather than reimplementing anything — the UI is a client of
the platform, not a second copy of it.
"""

from __future__ import annotations

import re
import uuid

import chainlit as cl

from aiplat import settings
from services.agent.agent import build_agent

# Emitted by aiplat.knowledge.search_knowledge_base for each passage.
PASSAGE_HEADER = re.compile(r"^\[(\d+)\] \(score ([\d.]+), source: (.+)\)$", re.MULTILINE)

REFUSAL_MARKERS = ("i don't know", "i do not know", "no relevant", "cannot answer")


def summarise_passages(tool_output: str) -> tuple[str, list[str]]:
    """Turn the retrieval tool's text into a step label and a source list."""
    matches = PASSAGE_HEADER.findall(tool_output or "")
    if not matches:
        return "No relevant passages found", []
    sources = [f"[{n}] {source}  ·  score {score}" for n, score, source in matches]
    return f"Found {len(matches)} passage(s)", sources


@cl.on_chat_start
async def start() -> None:
    # One session id per conversation so the agent's S3-backed state lines up
    # with what the user sees on screen.
    cl.user_session.set("session_id", f"ui-{uuid.uuid4().hex[:12]}")

    cfg = settings()
    if not cfg.retrieval_enabled:
        await cl.Message(
            content=(
                "**No knowledge base configured.** I can still talk, but I have "
                "nothing to search — so I will not be able to cite anything.\n\n"
                "Set `KNOWLEDGE_BASE_ID` in `.env` (`make env TENANT=<slug>`)."
            ),
            author="System",
        ).send()


@cl.on_message
async def answer(message: cl.Message) -> None:
    agent = build_agent(session_id=cl.user_session.get("session_id"))

    reply = cl.Message(content="")
    await reply.send()

    steps: dict[str, cl.Step] = {}
    tool_names: dict[str, str] = {}

    try:
        async for event in agent.stream_async(message.content):
            # A tool call has started: open a step so the search is visible while
            # it runs, not summarised after the fact.
            tool_use = event.get("current_tool_use") or {}
            tool_id = tool_use.get("toolUseId")
            if tool_id and tool_id not in steps:
                name = tool_use.get("name", "tool")
                tool_names[tool_id] = name
                step = cl.Step(name=_label(name), type="tool", default_open=False)
                await step.__aenter__()
                steps[tool_id] = step

            if result := event.get("tool_result"):
                await _close_step(steps, result)

            if chunk := event.get("data"):
                await reply.stream_token(chunk)

    except Exception as exc:  # noqa: BLE001 — anything that fails must reach the screen
        # Surfaced in the chat rather than only in the terminal — the person
        # running a demo is looking at the browser, not the logs.
        for step in steps.values():
            await step.__aexit__(type(exc), exc, None)
        await cl.Message(
            content=f"**Request failed:** `{type(exc).__name__}: {exc}`",
            author="System",
        ).send()
        return

    for step in steps.values():
        await step.__aexit__(None, None, None)

    await reply.update()

    if _looks_like_refusal(reply.content):
        # Not an error state. Saying "I don't know" when nothing supports an
        # answer is the behaviour the eval suite scores as refusal accuracy.
        await cl.Message(
            content="_Answered with a refusal — no supporting source was found._",
            author="System",
        ).send()


def _label(tool_name: str) -> str:
    return {
        "search_knowledge_base": "Searching the knowledge base",
    }.get(tool_name, tool_name)


async def _close_step(steps: dict[str, cl.Step], result: dict) -> None:
    tool_id = result.get("toolUseId")
    step = steps.pop(tool_id, None)
    if step is None:
        return

    text = "".join(block.get("text", "") for block in result.get("content", []))
    headline, sources = summarise_passages(text)
    step.output = headline + ("\n\n" + "\n".join(sources) if sources else "")
    await step.__aexit__(None, None, None)


def _looks_like_refusal(answer_text: str) -> bool:
    lowered = (answer_text or "").lower()
    return any(marker in lowered for marker in REFUSAL_MARKERS)
