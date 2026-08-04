"""Build an eval dataset from EnterpriseRAG-Bench.

Why this benchmark and not a better-known one: FinanceBench and Vectara's Open RAG
Benchmark are both CC-BY-NC. Non-commercial licensing is a problem for a repo meant
to be shown to customers. EnterpriseRAG-Bench is MIT, fully synthetic (a fictional
company, "Redwood Inference"), and its documents are runbooks, tickets and threads —
which is what internal corpora actually look like.

It also ships 20 questions typed `info_not_found`, which map directly onto this
harness's refusal cases. Those are the ones worth watching: an agent that scores well
on answerable questions but invents an answer to an unanswerable one is worse than
useless in an enterprise.

    python -m evals.datasets.fetch_enterprise_bench                 # confluence only
    python -m evals.datasets.fetch_enterprise_bench --sources confluence github

Source: https://github.com/onyx-dot-app/EnterpriseRAG-Bench (MIT)
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import re
import sys
import urllib.request
import zipfile
from collections import Counter
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

RELEASE_API = "https://api.github.com/repos/onyx-dot-app/EnterpriseRAG-Bench/releases/latest"
QUESTIONS_URL = (
    "https://raw.githubusercontent.com/onyx-dot-app/EnterpriseRAG-Bench/main/questions.jsonl"
)
SLICE_PATTERN = re.compile(r"^(?P<source>[a-z_]+)_slice_\d+\.zip$")

# Full corpus is ~512k documents across nine sources (1.2GB). Confluence alone is
# ~5k documents in 23MB — enough to exercise retrieval without a bill for embedding
# half a million documents.
DEFAULT_SOURCES = ["confluence"]
REFUSAL_TYPE = "info_not_found"


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def fetch_questions() -> list[dict]:
    logger.info("Fetching questions.jsonl")
    lines = _get(QUESTIONS_URL).decode("utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def slice_urls(sources: set[str]) -> list[tuple[str, str]]:
    """Release assets belonging to the requested sources, as (source, url) pairs."""
    release = json.loads(_get(RELEASE_API))
    assets = []
    for asset in release.get("assets", []):
        match = SLICE_PATTERN.match(asset["name"])
        if match and match.group("source") in sources:
            assets.append((match.group("source"), asset["browser_download_url"]))
    return sorted(assets)


def download_corpus(sources: set[str], out_dir: Path) -> set[str]:
    """Download and unpack document slices. Returns the doc_ids actually present."""
    assets = slice_urls(sources)
    if not assets:
        raise SystemExit(f"No release assets found for sources: {', '.join(sorted(sources))}")

    out_dir.mkdir(parents=True, exist_ok=True)
    doc_ids: set[str] = set()

    for source, url in assets:
        logger.info("Downloading %s", url.rsplit("/", 1)[-1])
        with zipfile.ZipFile(io.BytesIO(_get(url))) as archive:
            for name in archive.namelist():
                if name.endswith("/"):
                    continue
                filename = Path(name).name
                # Files are named dsid_<id>__<slug>.txt — the id is the join key
                # back to the questions' expected_doc_ids.
                doc_id = filename.split("__", 1)[0]
                doc_ids.add(doc_id)
                (out_dir / source).mkdir(exist_ok=True)
                (out_dir / source / filename).write_bytes(archive.read(name))

    logger.info("Unpacked %d documents into %s", len(doc_ids), out_dir)
    return doc_ids


def to_cases(questions: list[dict], sources: set[str], doc_ids: set[str]) -> list[dict]:
    """Convert benchmark questions into harness cases.

    A question is kept only when every document it needs is in the downloaded corpus.
    Keeping a question whose evidence was never ingested would score the agent on a
    document it could not possibly retrieve.
    """
    cases = []
    for q in questions:
        is_refusal = q["question_type"] == REFUSAL_TYPE
        needed = set(q.get("expected_doc_ids") or [])

        # Refusal questions carry empty source_types and expected_doc_ids by
        # construction — no document answers them. Filtering them by source would
        # drop all 20, which are the most valuable cases in the set.
        if not is_refusal and (not needed or not needed.issubset(doc_ids)):
            continue

        cases.append(
            {
                "id": q["question_id"],
                "question": q["question"],
                "expect_facts": [] if is_refusal else list(q.get("answer_facts") or []),
                "expect_refusal": is_refusal,
                "notes": f"{q['question_type']} | sources={','.join(q.get('source_types') or [])}",
            }
        )
    return cases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sources", nargs="+", default=DEFAULT_SOURCES, help="Which sources to download"
    )
    parser.add_argument("--corpus-dir", type=Path, default=Path("evals/corpus"))
    parser.add_argument("--out", type=Path, default=Path("evals/datasets/enterprise-bench.jsonl"))
    parser.add_argument("--limit", type=int, help="Cap the number of cases written")
    args = parser.parse_args(argv)

    sources = set(args.sources)
    questions = fetch_questions()
    doc_ids = download_corpus(sources, args.corpus_dir)
    cases = to_cases(questions, sources, doc_ids)

    if not cases:
        raise SystemExit(
            f"No questions are answerable from sources {sorted(sources)}. "
            f"Try --sources confluence github gmail."
        )

    dropped_by_limit = 0
    if args.limit and len(cases) > args.limit:
        dropped_by_limit = len(cases) - args.limit
        cases = cases[: args.limit]

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps(case, ensure_ascii=False) + "\n")

    by_type = Counter(c["notes"].split(" |")[0] for c in cases)
    logger.info("Wrote %d cases to %s", len(cases), args.out)
    for question_type, count in by_type.most_common():
        logger.info("  %-24s %d", question_type, count)

    # State what was left out. A pass rate over a trimmed corpus is not comparable to
    # the published benchmark, and a silent subset reads as full coverage.
    kept = {c["id"] for c in cases}
    logger.warning(
        "Kept %d of %d benchmark questions — the rest need sources not downloaded (%s).",
        len(kept),
        len(questions),
        ", ".join(sorted(sources)),
    )
    if dropped_by_limit:
        logger.warning("--limit dropped a further %d cases.", dropped_by_limit)
    logger.warning(
        "Retrieval over this subset is EASIER than the full 512k-document benchmark. "
        "Scores here are not comparable to published EnterpriseRAG-Bench results."
    )

    print(f"\n  Next: python -m services.ingest.ingest {args.corpus_dir} --wait")
    print(f"        python -m evals.run --dataset {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
