"""Eval harness — the thing that makes every later change measurable.

Built before any feature work on purpose. Without it, "does this prompt change help?"
is answered by vibes, and the question enterprise buyers actually ask — "how do you
know it is accurate?" — has no answer.

Three layers of scoring, deliberately:
  1. Retrieval quality (context recall, context precision). Scored against the
     retriever directly, not the answer — see below.
  2. Deterministic answer checks (keyword recall, citation present, refusal when
     expected). Cheap, stable, and they catch the regressions that matter most.
  3. LLM-as-judge for faithfulness. Useful but noisy — it is a tiebreaker, not truth,
     which is why it is reported separately rather than folded into one number.

Layer 1 exists because the other two only measure generation, and a RAG pipeline
fails in stages that fail independently: retrieval can miss the evidence entirely
and the answer still reads fine. Without a retrieval number, a bad score cannot
be attributed — you cannot tell a model that ignored good passages from a model
that never received them, and "is recall the ceiling?" stays a guess. That
question decides whether reranking is worth building, so the harness has to be
able to answer it.

**Retrieval is scored in isolation.** These metrics call `retrieve()` directly
with the question, rather than capturing what the agent actually searched for.
That is a deliberate trade: it measures the retriever as a component, so the
number moves when chunking, embeddings or top-k move and not when the prompt
changes. It does *not* capture the agent rewriting a query or searching several
times, so a healthy retriever here plus a poor answer means the loss is in the
agent, not the index — which is exactly the split the number is for.

    python -m evals.run --dataset evals/datasets/smoke.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from aiplat import build_model, prompts, settings
from aiplat.knowledge import retrieve
from services.agent.agent import SYSTEM_PROMPTS, ask

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

RESULTS_DIR = Path("evals/results")
CITATION = re.compile(r"\[\d+\]")
# Enough parallelism to be quick, low enough to stay under Bedrock throttling.
CONCURRENCY = 4


class Judgement(BaseModel):
    """Structured verdict so the judge cannot ramble instead of scoring."""

    faithful: bool = Field(description="Is every factual claim supported by the cited passages?")
    score: int = Field(ge=1, le=5, description="1 = wrong or unsupported, 5 = fully correct")
    covered_facts: list[int] = Field(
        default_factory=list,
        description="1-based indices of the expected facts the answer states correctly",
    )
    reason: str = Field(description="One sentence. What decided the score.")


@dataclass
class Case:
    id: str
    question: str
    # Substring matching. Cheap and stable, but only works when the answer must
    # contain a specific token.
    expect_keywords: list[str] = field(default_factory=list)
    # Full sentences the answer must convey. Scored by the judge, since the same
    # fact can be phrased a hundred ways. This is the better signal when available.
    expect_facts: list[str] = field(default_factory=list)
    expect_refusal: bool = False
    notes: str = ""

    @property
    def needs_judge(self) -> bool:
        return bool(self.expect_facts)


@dataclass
class CaseResult:
    id: str
    question: str
    answer: str
    passed: bool
    keyword_recall: float
    has_citation: bool
    refused: bool
    fact_recall: float | None = None
    judge_score: int | None = None
    judge_reason: str = ""
    # Retrieval, scored separately from the answer. None when retrieval is off, or
    # when the case carries no keywords to judge a passage against.
    passages_retrieved: int | None = None
    context_recall: float | None = None
    context_precision: float | None = None
    error: str = ""


def load_dataset(path: Path) -> list[Case]:
    cases = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            cases.append(Case(**json.loads(line)))
        except (json.JSONDecodeError, TypeError) as exc:
            raise SystemExit(f"{path}:{line_no} is not a valid case: {exc}")
    if not cases:
        raise SystemExit(f"{path} contains no cases")
    return cases


def _looks_like_refusal(answer: str) -> bool:
    lowered = answer.lower()
    markers = ("i don't know", "i do not know", "no relevant", "not found", "cannot answer")
    return any(m in lowered for m in markers)


def _keyword_recall(answer: str, keywords: list[str]) -> float:
    if not keywords:
        return 1.0
    lowered = answer.lower()
    hits = sum(1 for k in keywords if k.lower() in lowered)
    return hits / len(keywords)


def _passage_is_relevant(text: str, keywords: list[str]) -> bool:
    """A passage counts as relevant if it carries any expected keyword.

    Coarse on purpose. A judge could grade relevance per passage far more
    accurately, and would cost one model call per passage per case — enough that
    nobody would run the metric on every change, which is the only cadence at
    which a retrieval number is worth having.
    """
    lowered = text.lower()
    return any(k.lower() in lowered for k in keywords)


def context_recall(passages: list[dict], keywords: list[str]) -> float | None:
    """Of the evidence the answer needs, how much did retrieval actually return?

    This is the ceiling metric. If it is low, no amount of prompt work helps —
    the model is being asked to answer from passages that do not contain the
    answer, and its only correct move is to refuse.
    """
    if not keywords:
        return None
    blob = " ".join(p["text"] for p in passages).lower()
    return sum(1 for k in keywords if k.lower() in blob) / len(keywords)


def context_precision(passages: list[dict], keywords: list[str]) -> float | None:
    """Is the evidence near the top, or buried under plausible-looking noise?

    Rank-weighted (average precision), not a flat hit rate: a relevant passage at
    rank 1 and the same passage at rank 6 are different outcomes for a model
    reading top-down under a token budget. This is the number that says whether
    reranking would buy anything — flat precision cannot, because reranking does
    not change *which* passages are retrieved, only their order.
    """
    if not keywords or not passages:
        return None

    relevant = [_passage_is_relevant(p["text"], keywords) for p in passages]
    if not any(relevant):
        return 0.0

    hits = 0
    total = 0.0
    for rank, is_relevant in enumerate(relevant, start=1):
        if is_relevant:
            hits += 1
            total += hits / rank
    return total / hits


async def score_retrieval(case: Case) -> tuple[int | None, float | None, float | None]:
    """(passages, context_recall, context_precision) for one case.

    Retrieval failures are swallowed rather than failing the case: a knowledge
    base that is unreachable should show up as a missing retrieval number, not as
    a generation regression.
    """
    if not settings().retrieval_enabled:
        return None, None, None

    try:
        # retrieve() is sync and network-bound; keep it off the event loop so the
        # cases still run concurrently.
        passages = await asyncio.to_thread(retrieve, case.question)
    except Exception as exc:  # noqa: BLE001 — a scoring failure is not a case failure
        logger.warning("Retrieval scoring failed for %s: %s", case.id, exc)
        return None, None, None

    return (
        len(passages),
        context_recall(passages, case.expect_keywords),
        context_precision(passages, case.expect_keywords),
    )


async def judge(question: str, answer: str, expected_facts: list[str]) -> Judgement | None:
    """Second opinion on faithfulness and fact coverage.

    Its own agent so it shares no state, tools or conversation with the agent under
    test — a judge that can see the retrieved passages grades the retrieval, not the
    answer.
    """
    from strands import Agent

    critic = Agent(
        model=build_model(),
        system_prompt=(
            "You grade assistant answers. Judge whether claims are supported by the "
            "cited sources shown in the answer, and which of the expected facts the "
            "answer actually states. Do not reward length or confidence. Paraphrase "
            "counts as covering a fact; a near-miss on a number does not. An honest "
            "'I don't know' when sources are missing scores 5."
        ),
    )

    prompt = f"Question:\n{question}\n\nAnswer to grade:\n{answer}"
    if expected_facts:
        numbered = "\n".join(f"{i}. {f}" for i, f in enumerate(expected_facts, start=1))
        prompt += f"\n\nExpected facts:\n{numbered}"

    try:
        return await critic.structured_output_async(Judgement, prompt)
    except Exception:  # noqa: BLE001 — a failed judge must not abort the suite
        logger.warning("Judge failed for question: %s", question[:60])
        return None


async def run_case(case: Case, use_judge: bool, semaphore: asyncio.Semaphore) -> CaseResult:
    async with semaphore:
        # Started alongside the answer rather than after it: both are network-bound,
        # and the retrieval score does not depend on what the agent does with the
        # passages. Kept even when generation fails — knowing the retriever was
        # healthy is what separates a model outage from an index problem.
        retrieval = asyncio.create_task(score_retrieval(case))

        try:
            outcome = await ask(case.question)
            answer = outcome["answer"]
        except Exception as exc:
            logger.exception("Case %s failed", case.id)
            passages, ctx_recall, ctx_precision = await retrieval
            return CaseResult(
                id=case.id,
                question=case.question,
                answer="",
                passed=False,
                keyword_recall=0.0,
                has_citation=False,
                refused=False,
                passages_retrieved=passages,
                context_recall=ctx_recall,
                context_precision=ctx_precision,
                error=f"{type(exc).__name__}: {exc}",
            )

        passages, ctx_recall, ctx_precision = await retrieval
        refused = _looks_like_refusal(answer)
        recall = _keyword_recall(answer, case.expect_keywords)
        has_citation = bool(CITATION.search(answer))

        result = CaseResult(
            id=case.id,
            question=case.question,
            answer=answer,
            passed=False,
            keyword_recall=recall,
            has_citation=has_citation,
            refused=refused,
            passages_retrieved=passages,
            context_recall=ctx_recall,
            context_precision=ctx_precision,
        )

        # Fact-graded cases always need the judge; without it there is nothing to
        # score them on, so the flag is ignored rather than silently passing them.
        if use_judge or case.needs_judge:
            verdict = await judge(case.question, answer, case.expect_facts)
            if verdict:
                result.judge_score = verdict.score
                result.judge_reason = verdict.reason
                if case.expect_facts:
                    covered = {i for i in verdict.covered_facts if 1 <= i <= len(case.expect_facts)}
                    result.fact_recall = len(covered) / len(case.expect_facts)

        if case.expect_refusal:
            # The only correct behaviour is admitting ignorance.
            result.passed = refused
        elif result.fact_recall is not None:
            # Every expected fact must be present. These are single-answer questions,
            # so a partial answer is a wrong answer.
            result.passed = result.fact_recall == 1.0 and not refused
        else:
            result.passed = recall >= 0.5 and not refused

        status = "PASS" if result.passed else "FAIL"
        scored = (
            f"facts={result.fact_recall:.2f}"
            if result.fact_recall is not None
            else f"recall={recall:.2f}"
        )
        ctx = f" ctx={ctx_recall:.2f}" if ctx_recall is not None else ""
        logger.info("%s  %-14s %s%s cite=%s", status, case.id, scored, ctx, has_citation)
        return result


async def run(dataset: Path, use_judge: bool) -> dict:
    cases = load_dataset(dataset)
    logger.info("Running %d cases from %s", len(cases), dataset)

    semaphore = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(run_case(c, use_judge, semaphore) for c in cases))

    passed = sum(1 for r in results if r.passed)
    errored = sum(1 for r in results if r.error)
    judged = [r.judge_score for r in results if r.judge_score is not None]
    facts = [r.fact_recall for r in results if r.fact_recall is not None]

    # Refusal cases are reported on their own: an agent can score well overall while
    # being unable to say "I don't know", and that failure is the expensive one.
    unanswerable = {c.id for c in cases if c.expect_refusal}
    refusal_cases = [r for r in results if r.id in unanswerable]
    refusal_passed = sum(1 for r in refusal_cases if r.passed)

    # Retrieval is scored on answerable cases only. On an unanswerable question the
    # expected evidence does not exist, so recall against it is meaningless — what
    # matters there is measured separately, below.
    answerable = [r for r in results if r.id not in unanswerable]
    recalls = [r.context_recall for r in answerable if r.context_recall is not None]
    precisions = [r.context_precision for r in answerable if r.context_precision is not None]

    # How often the retriever hands the model passages for a question the corpus
    # cannot answer. Every one of those is plausible-looking noise the model has to
    # refuse anyway — so a high number here means refusal accuracy is carrying
    # weight that belongs to the retrieval threshold.
    noisy = [r for r in refusal_cases if r.passages_retrieved]

    return {
        "dataset": str(dataset),
        # A score with no prompt version attached cannot answer "did this prompt
        # change help?", which is the question this harness exists to answer.
        "prompt": prompts.load(SYSTEM_PROMPTS, settings().prompt_version).label,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "errored": errored,
        "pass_rate": round(passed / len(results), 3),
        "citation_rate": round(sum(1 for r in results if r.has_citation) / len(results), 3),
        "mean_fact_recall": round(sum(facts) / len(facts), 3) if facts else None,
        "mean_judge_score": round(sum(judged) / len(judged), 2) if judged else None,
        "refusal_accuracy": (
            round(refusal_passed / len(refusal_cases), 3) if refusal_cases else None
        ),
        # Retrieval. Low recall is a ceiling no prompt change can lift; low precision
        # with healthy recall is the case reranking exists to fix.
        "context_recall": round(sum(recalls) / len(recalls), 3) if recalls else None,
        "context_precision": (
            round(sum(precisions) / len(precisions), 3) if precisions else None
        ),
        "unanswerable_retrieval_rate": (
            round(len(noisy) / len(refusal_cases), 3) if refusal_cases else None
        ),
        "cases": [asdict(r) for r in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the agent against an eval dataset")
    parser.add_argument("--dataset", type=Path, default=Path("evals/datasets/smoke.jsonl"))
    parser.add_argument("--judge", action="store_true", help="Add LLM-as-judge faithfulness scoring")
    parser.add_argument("--out", type=Path, help="Where to write the JSON report")
    args = parser.parse_args(argv)

    if not args.dataset.exists():
        raise SystemExit(f"Dataset not found: {args.dataset}")

    report = asyncio.run(run(args.dataset, args.judge))

    out = args.out or RESULTS_DIR / f"{args.dataset.stem}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    print(f"  pass rate         {report['pass_rate']:.0%}  ({report['passed']}/{report['total']})")
    print(f"  citation rate     {report['citation_rate']:.0%}")
    if report["mean_fact_recall"] is not None:
        print(f"  fact recall       {report['mean_fact_recall']:.0%}")
    if report["refusal_accuracy"] is not None:
        print(f"  refusal accuracy  {report['refusal_accuracy']:.0%}")

    # Printed apart from the generation numbers, because the whole point is being
    # able to tell the two halves of the pipeline apart.
    if report["context_recall"] is not None:
        print()
        print(f"  context recall    {report['context_recall']:.0%}   evidence retrieved at all")
        print(f"  context precision {report['context_precision']:.0%}   and ranked near the top")
        if report["unanswerable_retrieval_rate"] is not None:
            print(
                f"  noise on N/A      {report['unanswerable_retrieval_rate']:.0%}"
                f"   unanswerable questions that still returned passages"
            )
    if report["mean_judge_score"] is not None:
        print(f"  judge score       {report['mean_judge_score']}/5")
    if report["errored"]:
        print(f"  errored           {report['errored']}")
    print(f"  report            {out}")

    # Non-zero exit so this can gate a pipeline.
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
