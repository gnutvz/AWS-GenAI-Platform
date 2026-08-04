"""Eval harness — the thing that makes every later change measurable.

Built before any feature work on purpose. Without it, "does this prompt change help?"
is answered by vibes, and the question enterprise buyers actually ask — "how do you
know it is accurate?" — has no answer.

Two layers of scoring, deliberately:
  1. Deterministic checks (keyword recall, citation present, refusal when expected).
     Cheap, stable, and they catch the regressions that matter most.
  2. LLM-as-judge for faithfulness. Useful but noisy — it is a tiebreaker, not truth,
     which is why it is reported separately rather than folded into one number.

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

from aiplat import build_model
from services.agent.agent import ask

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
    reason: str = Field(description="One sentence. What decided the score.")


@dataclass
class Case:
    id: str
    question: str
    expect_keywords: list[str] = field(default_factory=list)
    expect_refusal: bool = False
    notes: str = ""


@dataclass
class CaseResult:
    id: str
    question: str
    answer: str
    passed: bool
    keyword_recall: float
    has_citation: bool
    refused: bool
    judge_score: int | None = None
    judge_reason: str = ""
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


async def judge(question: str, answer: str) -> Judgement | None:
    """Second opinion on faithfulness. Its own agent so it shares no state or tools."""
    from strands import Agent

    critic = Agent(
        model=build_model(),
        system_prompt=(
            "You grade assistant answers for faithfulness. Judge only whether claims are "
            "supported by the cited sources shown in the answer. Do not reward length or "
            "confidence. An honest 'I don't know' when sources are missing scores 5."
        ),
    )
    try:
        return await critic.structured_output_async(
            Judgement,
            f"Question:\n{question}\n\nAnswer to grade:\n{answer}",
        )
    except Exception:  # noqa: BLE001 — a failed judge must not abort the suite
        logger.warning("Judge failed for question: %s", question[:60])
        return None


async def run_case(case: Case, use_judge: bool, semaphore: asyncio.Semaphore) -> CaseResult:
    async with semaphore:
        try:
            outcome = await ask(case.question, tenant="eval")
            answer = outcome["answer"]
        except Exception as exc:
            logger.exception("Case %s failed", case.id)
            return CaseResult(
                id=case.id,
                question=case.question,
                answer="",
                passed=False,
                keyword_recall=0.0,
                has_citation=False,
                refused=False,
                error=f"{type(exc).__name__}: {exc}",
            )

        refused = _looks_like_refusal(answer)
        recall = _keyword_recall(answer, case.expect_keywords)
        has_citation = bool(CITATION.search(answer))

        if case.expect_refusal:
            # The only correct behaviour is admitting ignorance.
            passed = refused
        else:
            passed = recall >= 0.5 and not refused

        result = CaseResult(
            id=case.id,
            question=case.question,
            answer=answer,
            passed=passed,
            keyword_recall=recall,
            has_citation=has_citation,
            refused=refused,
        )

        if use_judge:
            verdict = await judge(case.question, answer)
            if verdict:
                result.judge_score = verdict.score
                result.judge_reason = verdict.reason

        status = "PASS" if passed else "FAIL"
        logger.info("%s  %-14s recall=%.2f cite=%s", status, case.id, recall, has_citation)
        return result


async def run(dataset: Path, use_judge: bool) -> dict:
    cases = load_dataset(dataset)
    logger.info("Running %d cases from %s", len(cases), dataset)

    semaphore = asyncio.Semaphore(CONCURRENCY)
    results = await asyncio.gather(*(run_case(c, use_judge, semaphore) for c in cases))

    passed = sum(1 for r in results if r.passed)
    errored = sum(1 for r in results if r.error)
    judged = [r.judge_score for r in results if r.judge_score is not None]

    return {
        "dataset": str(dataset),
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "errored": errored,
        "pass_rate": round(passed / len(results), 3),
        "citation_rate": round(sum(1 for r in results if r.has_citation) / len(results), 3),
        "mean_judge_score": round(sum(judged) / len(judged), 2) if judged else None,
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
    print(f"  pass rate      {report['pass_rate']:.0%}  ({report['passed']}/{report['total']})")
    print(f"  citation rate  {report['citation_rate']:.0%}")
    if report["mean_judge_score"] is not None:
        print(f"  judge score    {report['mean_judge_score']}/5")
    if report["errored"]:
        print(f"  errored        {report['errored']}")
    print(f"  report         {out}")

    # Non-zero exit so this can gate a pipeline.
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
