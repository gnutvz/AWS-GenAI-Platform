"""Retrieval against a Bedrock Knowledge Base, exposed as a Strands tool.

Retrieve (not RetrieveAndGenerate) on purpose: the agent should decide what to do
with the passages. RetrieveAndGenerate hides the generation step, which makes the
answer harder to trace and impossible to combine with other tools in one turn.

Hybrid search is the default because pure semantic search reliably misses exact
identifiers — part numbers, error codes, model names — which is most of what
enterprise users actually search for.

On filters, and why they are not a tool argument
------------------------------------------------
The ingest pipeline writes a metadata sidecar next to every document, so
retrieval can be narrowed by any attribute it carries. The obvious way to expose
that is a `filters` parameter on the tool — and it is the wrong one, because the
model is a caller like any other. A filter the model can set is a filter the
model can drop, and prompt injection in a retrieved passage is enough to make it
want to.

So filters are bound when the agent is constructed, by the code that knows who
is asking, and the tool the model sees has no way to name them. Same argument as
the tenant: security parameters come from the deployment or the request context,
never from something a language model chooses. `make_search_tool()` is what
enforces the difference.

Nothing supplies filters yet — end-user identity does not exist here (see
docs/platform.md). This is the seam it will use, built now because the metadata
it reads had to be written at ingest time either way.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

import boto3
from strands import tool

from aiplat.aws import boto_config
from aiplat.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 6
# Below this, passages are usually topically adjacent but not actually answering.
MIN_SCORE = 0.35

# Metadata attributes a passage must match, e.g. {"doc_type": "technical"}.
Filters = dict[str, str]


@lru_cache(maxsize=1)
def _client():
    # Shorter read timeout than the platform default: retrieval that has not
    # answered in twenty seconds is not about to, and the agent is holding a
    # user's request open while it waits.
    return boto3.client(
        "bedrock-agent-runtime",
        region_name=settings().region,
        config=boto_config(read_timeout=20),
    )


def as_bedrock_filter(filters: Filters) -> dict[str, Any]:
    """Turn `{"doc_type": "technical"}` into the shape Bedrock expects.

    Only equality and AND, deliberately. Retrieval filters here exist to narrow
    what a caller is allowed to see, and every operator added to that grammar is
    another way to write a filter that looks restrictive and is not.
    """
    clauses = [{"equals": {"key": key, "value": value}} for key, value in sorted(filters.items())]
    return clauses[0] if len(clauses) == 1 else {"andAll": clauses}


def retrieve(
    query: str, top_k: int = DEFAULT_TOP_K, filters: Filters | None = None
) -> list[dict[str, Any]]:
    """Raw retrieval. Returns passages sorted by relevance, highest first.

    Args:
        query: What to search for.
        top_k: How many passages to ask the knowledge base for.
        filters: Metadata attributes every passage must match. Supplied by the
            caller that knows who is asking — never by the model. See the module
            docstring.
    """
    cfg = settings()

    vector_search: dict[str, Any] = {
        "numberOfResults": top_k,
        "overrideSearchType": "HYBRID",
    }
    if filters:
        vector_search["filter"] = as_bedrock_filter(filters)

    response = _client().retrieve(
        knowledgeBaseId=cfg.require("knowledge_base_id"),
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": vector_search},
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


def make_search_tool(filters: Filters | None = None):
    """Build the retrieval tool, with any filters baked in.

    The returned tool takes a query and a result count — and no way to reach the
    filters, which live in this closure. That is the entire point: whoever calls
    `make_search_tool` decides what this agent may see, and the model on the
    other side cannot widen it, whether by reasoning its way there or by reading
    an instruction someone planted in a document.
    """

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
            passages = retrieve(query, top_k, filters=filters)
        except Exception as exc:  # surfaced to the model, which can retry or say it failed
            logger.exception("Knowledge base retrieval failed")
            return f"Retrieval failed: {exc}"

        if not passages:
            return "No relevant passages found. Say so rather than guessing."

        return "\n\n".join(
            f"[{i}] (score {p['score']:.2f}, source: {p['source']})\n{p['text']}"
            for i, p in enumerate(passages, start=1)
        )

    return search_knowledge_base


# The unfiltered tool, for callers that need no narrowing. Kept as a module-level
# name because it reads better at the import site than `make_search_tool()`.
search_knowledge_base = make_search_tool()
