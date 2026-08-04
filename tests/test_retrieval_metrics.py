"""Retrieval scoring — the half of the pipeline the eval suite could not see.

Fact recall, refusal accuracy and citation rate all measure generation. A RAG
pipeline fails in stages that fail independently, so those three cannot tell a
model that ignored good passages from a model that never received them.

That gap had a concrete cost: `docs/architecture.drawio` names the trigger for
building reranking as "eval shows recall, not generation, is the ceiling" — a
condition the eval suite had no way to evaluate. These two numbers are what make
that decision measurable rather than a guess.

The split they draw:
  low recall                 → the evidence is not being retrieved; no prompt or
                               model change can fix it, and refusing is correct
  healthy recall, low precision → the evidence is there but buried; this is the
                               case reranking exists to fix
  both healthy, bad answers  → the loss is in the agent, not the index
"""

from __future__ import annotations

import asyncio

import pytest

from aiplat import config
from evals.run import Case, context_precision, context_recall, score_retrieval


def passages(*texts: str) -> list[dict]:
    """Retrieval output, reduced to the field the metrics read."""
    return [{"text": t, "score": 0.9, "source": "s3://b/doc.md"} for t in texts]


KEYWORDS = ["warranty", "24 months"]


class TestContextRecall:
    def test_all_evidence_present(self):
        found = passages("The warranty is 24 months from delivery.")
        assert context_recall(found, KEYWORDS) == 1.0

    def test_partial_evidence(self):
        found = passages("The warranty terms are described in section 4.")
        assert context_recall(found, KEYWORDS) == 0.5

    def test_evidence_spread_across_passages_still_counts(self):
        """Recall asks whether the model *could* answer, not from one passage."""
        found = passages("Warranty policy overview.", "Coverage lasts 24 months.")
        assert context_recall(found, KEYWORDS) == 1.0

    def test_nothing_retrieved_is_zero_not_undefined(self):
        """An empty result is a measured failure, not a missing measurement."""
        assert context_recall([], KEYWORDS) == 0.0

    def test_case_insensitive(self):
        assert context_recall(passages("WARRANTY: 24 MONTHS"), KEYWORDS) == 1.0

    def test_no_keywords_means_unmeasurable(self):
        """None, not 1.0 — a case with no ground truth must not inflate the mean."""
        assert context_recall(passages("anything"), []) is None


class TestContextPrecision:
    def test_relevant_passage_first_scores_perfectly(self):
        found = passages("The warranty is 24 months.", "Unrelated.", "Also unrelated.")
        assert context_precision(found, KEYWORDS) == 1.0

    def test_rank_matters(self):
        """The whole reason for average precision over a flat hit rate.

        Same passages, same recall, same number of relevant hits — only the order
        differs. A flat precision@k cannot see this, which would make it useless
        for deciding whether reranking is worth building.
        """
        first = context_precision(
            passages("The warranty is 24 months.", "Noise.", "Noise."), KEYWORDS
        )
        last = context_precision(
            passages("Noise.", "Noise.", "The warranty is 24 months."), KEYWORDS
        )
        assert first > last
        assert first == 1.0
        assert last == pytest.approx(1 / 3)

    def test_several_relevant_passages_average_their_ranks(self):
        found = passages("Warranty details.", "Noise.", "It lasts 24 months.")
        # Hits at rank 1 and rank 3 → (1/1 + 2/3) / 2
        assert context_precision(found, KEYWORDS) == pytest.approx((1.0 + 2 / 3) / 2)

    def test_nothing_relevant_scores_zero(self):
        assert context_precision(passages("Noise.", "More noise."), KEYWORDS) == 0.0

    def test_empty_retrieval_is_unmeasurable(self):
        """Distinct from scoring zero: recall already records that nothing came back."""
        assert context_precision([], KEYWORDS) is None

    def test_no_keywords_means_unmeasurable(self):
        assert context_precision(passages("anything"), []) is None


class TestScoringDegradesQuietly:
    """A retrieval score is diagnostic, so its absence must not fail a case."""

    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        config.settings.cache_clear()
        yield
        config.settings.cache_clear()

    def test_no_knowledge_base_scores_nothing(self, monkeypatch):
        monkeypatch.delenv("KNOWLEDGE_BASE_ID", raising=False)
        config.settings.cache_clear()

        result = asyncio.run(score_retrieval(Case(id="c1", question="q?")))

        assert result == (None, None, None)

    def test_a_failing_retriever_does_not_fail_the_case(self, monkeypatch):
        """An unreachable index should read as a missing number, not a regression."""
        monkeypatch.setenv("KNOWLEDGE_BASE_ID", "kb-test")
        config.settings.cache_clear()

        def boom(*args, **kwargs):
            raise RuntimeError("ThrottlingException")

        monkeypatch.setattr("evals.run.retrieve", boom)

        assert asyncio.run(score_retrieval(Case(id="c1", question="q?"))) == (None, None, None)

    def test_scores_against_what_the_retriever_returned(self, monkeypatch):
        monkeypatch.setenv("KNOWLEDGE_BASE_ID", "kb-test")
        config.settings.cache_clear()
        monkeypatch.setattr(
            "evals.run.retrieve",
            lambda *a, **k: passages("The warranty is 24 months.", "Noise."),
        )

        case = Case(id="c1", question="warranty?", expect_keywords=KEYWORDS)
        assert asyncio.run(score_retrieval(case)) == (2, 1.0, 1.0)


class TestTheSignalTheyProduceTogether:
    """The three diagnoses the pair is meant to separate."""

    def test_retrieval_ceiling_reads_as_low_recall(self):
        found = passages("An unrelated policy document.", "Another unrelated one.")
        assert context_recall(found, KEYWORDS) == 0.0
        assert context_precision(found, KEYWORDS) == 0.0

    def test_ranking_problem_reads_as_healthy_recall_low_precision(self):
        """The case that justifies a reranker — the evidence is there, just buried."""
        found = passages("Noise.", "Noise.", "Noise.", "The warranty is 24 months.")
        assert context_recall(found, KEYWORDS) == 1.0
        assert context_precision(found, KEYWORDS) == pytest.approx(0.25)

    def test_healthy_retrieval_reads_as_both_high(self):
        found = passages("The warranty is 24 months.", "Noise.")
        assert context_recall(found, KEYWORDS) == 1.0
        assert context_precision(found, KEYWORDS) == 1.0
