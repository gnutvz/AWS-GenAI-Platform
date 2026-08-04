"""Generate an eval dataset from a tenant's own corpus.

Every customer asks "how do I know it is accurate?" and the honest answer needs
numbers measured on *their* documents, not on a public benchmark. Writing those
questions by hand takes days per corpus, which is why most pilots ship without
them and argue about quality from vibes.

This reads the documents, samples passages, and asks a model for two things per
passage:

  1. A question the passage answers, plus the facts a correct answer must state.
  2. A question the passage *nearly* answers but does not — the passage mentions
     a threshold without giving its value, references a procedure without the
     steps. These are the refusal cases, and they are the point. An agent that
     scores well on answerable questions while inventing answers to these is
     worse than no agent at all.

Unanswerable questions are the hard part to get right, so `--verify` checks each
one against the live knowledge base and drops any that retrieval can actually
answer from somewhere else in the corpus.

    python -m evals.generate --tenant acme --passages 40
    python -m evals.generate --tenant acme --passages 40 --verify

Costs one model call per passage — cents for a typical run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from aiplat import build_model
from aiplat.tenants import Tenant
from aiplat.tenants import get as get_tenant
from services.ingest.ingest import documents_under, parse_to_markdown

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Long enough to contain a specific claim, short enough that the question is
# about one thing. Passages below the floor are usually headings or boilerplate.
MIN_PASSAGE_CHARS = 400
MAX_PASSAGE_CHARS = 2500
CONCURRENCY = 4

# Retrieval score above which an "unanswerable" question is considered answerable
# after all. Matches the floor the agent's own retrieval tool applies.
ANSWERABLE_SCORE = 0.35


class GeneratedPair(BaseModel):
    """One answerable question and one deliberately unanswerable neighbour."""

    question: str = Field(description="A specific question this passage fully answers")
    facts: list[str] = Field(
        description=(
            "Each fact a correct answer must state, as a complete sentence. "
            "Two or three at most — this is the grading key, not a summary."
        )
    )
    unanswerable_question: str = Field(
        description=(
            "A question about the same subject that this passage does NOT answer, "
            "because the specific detail asked for is absent. It must sound "
            "reasonable to someone who has read the passage."
        )
    )
    unanswerable_reason: str = Field(
        description="One sentence: which detail is missing from the passage."
    )


SYSTEM_PROMPT = """You write evaluation questions for a document retrieval system.

For the passage you are given, produce:

1. A question the passage fully and unambiguously answers. Prefer specific facts
   — numbers, names, thresholds, procedures — over general themes. Someone
   holding this passage must be able to answer it exactly.

2. The facts a correct answer must state, as complete sentences.

3. A question that is clearly about the same subject but that the passage does
   NOT contain the answer to. The best of these ask for a level of detail the
   passage stops short of: it names a threshold without the value, refers to an
   approval step without saying who approves, mentions a limit without the
   number. It must NOT be about an unrelated topic, and must not be answerable
   by guessing from the passage.

Never invent facts. Everything in the answerable question and its facts must be
explicitly present in the passage.
"""


@dataclass
class Passage:
    source: str
    text: str


def split_passages(markdown: str) -> list[str]:
    """Split on blank lines, then merge until each block is substantial.

    Deliberately crude: this feeds question generation, not retrieval, so the
    boundaries only need to keep one topic together.
    """
    blocks = [b.strip() for b in markdown.split("\n\n") if b.strip()]
    passages: list[str] = []
    buffer = ""

    for block in blocks:
        buffer = f"{buffer}\n\n{block}".strip() if buffer else block
        if len(buffer) >= MIN_PASSAGE_CHARS:
            passages.append(buffer[:MAX_PASSAGE_CHARS])
            buffer = ""

    if len(buffer) >= MIN_PASSAGE_CHARS:
        passages.append(buffer)
    return passages


def collect_passages(tenant: Tenant, limit: int, seed: int) -> list[Passage]:
    passages: list[Passage] = []

    for source in tenant.sources:
        root = Path(source.path)
        if not root.exists():
            logger.warning("Source path missing, skipping: %s", root)
            continue
        for path in documents_under(root):
            for text in split_passages(parse_to_markdown(path)):
                passages.append(Passage(source=path.name, text=text))

    if not passages:
        raise SystemExit(
            f"No usable passages found for tenant {tenant.slug!r}. "
            f"Check the source paths in tenants/{tenant.slug}.yaml."
        )

    # Sampled rather than taken in order: the first N passages of a corpus are
    # usually the first few documents, which makes the eval set unrepresentative.
    random.Random(seed).shuffle(passages)
    return passages[:limit]


async def generate_one(passage: Passage, semaphore: asyncio.Semaphore) -> GeneratedPair | None:
    from strands import Agent

    async with semaphore:
        writer = Agent(model=build_model(), system_prompt=SYSTEM_PROMPT)
        try:
            return await writer.structured_output_async(
                GeneratedPair, f"Passage from {passage.source}:\n\n{passage.text}"
            )
        except Exception:  # noqa: BLE001 — one bad passage must not lose the run
            logger.warning("Generation failed for a passage from %s", passage.source)
            return None


async def verify_unanswerable(cases: list[dict]) -> list[dict]:
    """Drop refusal cases the corpus can actually answer.

    A question invented as unanswerable from one passage may be answered by a
    different document. Left in, it would punish the agent for being right —
    which is worse than having fewer refusal cases.
    """
    from aiplat.knowledge import retrieve

    kept: list[dict] = []
    dropped = 0

    for case in cases:
        if not case["expect_refusal"]:
            kept.append(case)
            continue
        try:
            passages = retrieve(case["question"], top_k=3)
        except Exception:  # noqa: BLE001 — verification is best-effort
            logger.warning("Could not verify %s; keeping it", case["id"])
            kept.append(case)
            continue

        if passages and passages[0]["score"] >= ANSWERABLE_SCORE:
            dropped += 1
            logger.info(
                "Dropped %s — corpus answers it after all (score %.2f, %s)",
                case["id"],
                passages[0]["score"],
                passages[0]["source"],
            )
            continue
        kept.append(case)

    if dropped:
        logger.warning("Verification dropped %d refusal case(s) as answerable", dropped)
    return kept


def to_cases(results: list[tuple[Passage, GeneratedPair]], refusal_ratio: float) -> list[dict]:
    cases: list[dict] = []
    refusal_budget = int(len(results) * refusal_ratio)

    for index, (passage, pair) in enumerate(results, start=1):
        cases.append(
            {
                "id": f"gen-{index:03d}",
                "question": pair.question,
                "expect_facts": pair.facts,
                "expect_refusal": False,
                "notes": f"generated from {passage.source}",
            }
        )
        if index <= refusal_budget:
            cases.append(
                {
                    "id": f"gen-{index:03d}-nf",
                    "question": pair.unanswerable_question,
                    "expect_facts": [],
                    "expect_refusal": True,
                    "notes": f"unanswerable — {pair.unanswerable_reason}",
                }
            )
    return cases


async def build(tenant: Tenant, passages: int, seed: int, refusal_ratio: float) -> list[dict]:
    sampled = collect_passages(tenant, passages, seed)
    logger.info("Generating from %d passages", len(sampled))

    semaphore = asyncio.Semaphore(CONCURRENCY)
    generated = await asyncio.gather(*(generate_one(p, semaphore) for p in sampled))

    results = [(p, g) for p, g in zip(sampled, generated, strict=True) if g is not None]
    if not results:
        raise SystemExit("Every generation failed — check model access and credentials.")

    logger.info("Generated %d question pairs", len(results))
    return to_cases(results, refusal_ratio)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", required=True, help="Which tenant's corpus to read")
    parser.add_argument("--passages", type=int, default=40, help="How many passages to sample")
    parser.add_argument(
        "--refusal-ratio",
        type=float,
        default=0.3,
        help="Fraction of passages that also contribute an unanswerable question",
    )
    parser.add_argument("--seed", type=int, default=0, help="Sampling seed, for reproducibility")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Check unanswerable questions against the live knowledge base",
    )
    parser.add_argument("--out", type=Path, help="Defaults to evals/datasets/<tenant>.jsonl")
    args = parser.parse_args(argv)

    tenant = get_tenant(args.tenant)
    cases = asyncio.run(build(tenant, args.passages, args.seed, args.refusal_ratio))

    if args.verify:
        cases = asyncio.run(verify_unanswerable(cases))

    out = args.out or Path(f"evals/datasets/{tenant.slug}.jsonl")
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        fh.write(f"# Generated from the {tenant.display_name} corpus. Review before trusting.\n")
        for case in cases:
            fh.write(json.dumps(case, ensure_ascii=False) + "\n")

    answerable = sum(1 for c in cases if not c["expect_refusal"])
    refusal = len(cases) - answerable
    print(f"\n  {out}")
    print(f"  {answerable} answerable, {refusal} unanswerable")
    if not args.verify:
        print("  Not verified — run with --verify once the corpus is indexed.")
    print("\n  These are machine-written. Read them before quoting the score to anyone.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
