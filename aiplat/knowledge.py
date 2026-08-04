"""Retrieval against a Bedrock Knowledge Base, exposed as a Strands tool.

Retrieve (not RetrieveAndGenerate) on purpose: the agent should decide what to do
with the passages. RetrieveAndGenerate hides the generation step, which makes the
answer harder to trace and impossible to combine with other tools in one turn.

Hybrid search is the default because pure semantic search reliably misses exact
identifiers — part numbers, error codes, model names — which is most of what
enterprise users actually search for.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import boto3
from strands import tool

from aiplat.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 6
# Below this, passages are usually topically adjacent but not actually answering.
MIN_SCORE = 0.35


@lru_cache(maxsize=1)
def _client():
    return boto3.client("bedrock-agent-runtime", region_name=settings().region)


def retrieve(query: str, top_k: int = DEFAULT_TOP_K) -> list[dict[str, Any]]:
    """Raw retrieval. Returns passages sorted by relevance, highest first."""
    cfg = settings()
    response = _client().retrieve(
        knowledgeBaseId=cfg.require("knowledge_base_id"),
        retrievalQuery={"text": query},
        retrievalConfiguration={
            "vectorSearchConfiguration": {
                "numberOfResults": top_k,
                "overrideSearchType": "HYBRID",
            }
        },
    )

    passages = []
    for item in response.get("retrievalResults", []):
        score = item.get("score", 0.0)
        if score < MIN_SCORE:
            continue
        passages.append(
            {
                "text": item.get("content", {}).get("text", ""),
                "score": score,
                "source": _source_of(item),
            }
        )
    return passages


def _source_of(item: dict[str, Any]) -> str:
    """A citation the user can actually follow back to a document."""
    location = item.get("location", {})
    for key in ("s3Location", "webLocation", "confluenceLocation", "sharePointLocation"):
        if key in location:
            loc = location[key]
            return loc.get("uri") or loc.get("url") or "unknown"
    return item.get("documentId") or "unknown"


@tool
def search_knowledge_base(query: str, top_k: int = DEFAULT_TOP_K) -> str:
    """Search internal documentation for passages relevant to a question.

    Use this before answering anything about internal products, policies, or
    procedures. Prefer specific queries over broad ones.

    Args:
        query: What to search for, phrased as a question or keyword string.
        top_k: How many passages to return. Raise it for broad questions.
    """
    try:
        passages = retrieve(query, top_k)
    except Exception as exc:  # surfaced to the model, which can retry or say it failed
        logger.exception("Knowledge base retrieval failed")
        return f"Retrieval failed: {exc}"

    if not passages:
        return "No relevant passages found. Say so rather than guessing."

    return "\n\n".join(
        f"[{i}] (score {p['score']:.2f}, source: {p['source']})\n{p['text']}"
        for i, p in enumerate(passages, start=1)
    )
