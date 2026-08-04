"""Eval generation.

The generator writes the yardstick everything else is measured against, so a
quiet bug here produces confident numbers about nothing. Model calls are stubbed;
what is tested is the sampling, the shaping and the verification gate.
"""

from __future__ import annotations

import pytest

from aiplat.tenants import Source, Tenant
from evals import generate


def passage(source: str = "doc.md", text: str = "x") -> generate.Passage:
    return generate.Passage(source=source, text=text)


def pair(n: int = 1) -> generate.GeneratedPair:
    return generate.GeneratedPair(
        question=f"What is setting {n}?",
        facts=[f"Setting {n} is 42."],
        unanswerable_question=f"What is the exact value of the {n} threshold?",
        unanswerable_reason="The passage names the threshold without giving its value.",
    )


class TestPassageSplitting:
    def test_merges_short_blocks_until_substantial(self):
        markdown = "\n\n".join(["short line"] * 60)
        passages = generate.split_passages(markdown)
        assert passages
        assert all(len(p) >= generate.MIN_PASSAGE_CHARS for p in passages)

    def test_drops_a_trailing_fragment(self):
        """A leftover heading makes a question about nothing."""
        markdown = ("x" * 500) + "\n\ntiny tail"
        assert all("tiny tail" not in p for p in generate.split_passages(markdown))

    def test_caps_passage_length(self):
        passages = generate.split_passages("y" * 10_000)
        assert all(len(p) <= generate.MAX_PASSAGE_CHARS for p in passages)

    def test_boilerplate_only_document_yields_nothing(self):
        assert generate.split_passages("# Title\n\nTOC\n") == []


class TestSampling:
    @pytest.fixture
    def corpus(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        for i in range(6):
            (docs / f"doc{i}.md").write_text("z" * 600, encoding="utf-8")
        return Tenant(
            slug="acme",
            display_name="Acme",
            sources=[Source(path=str(docs))],
        )

    def test_respects_the_limit(self, corpus):
        assert len(generate.collect_passages(corpus, limit=3, seed=0)) == 3

    def test_sampling_is_reproducible(self, corpus):
        first = generate.collect_passages(corpus, limit=4, seed=7)
        second = generate.collect_passages(corpus, limit=4, seed=7)
        assert [p.source for p in first] == [p.source for p in second]

    def test_does_not_just_take_the_first_documents(self, corpus):
        """Reading in order makes an eval set about the first file only."""
        ordered = sorted(p.source for p in generate.collect_passages(corpus, 6, seed=0))
        sampled = [p.source for p in generate.collect_passages(corpus, 6, seed=0)]
        assert sampled != ordered

    def test_empty_corpus_fails_loudly(self, tmp_path):
        empty = Tenant(slug="acme", display_name="Acme", sources=[Source(path=str(tmp_path))])
        with pytest.raises(SystemExit, match="No usable passages"):
            generate.collect_passages(empty, limit=5, seed=0)

    def test_missing_source_path_is_skipped_not_fatal(self, corpus, tmp_path):
        corpus = Tenant(
            slug="acme",
            display_name="Acme",
            sources=[*corpus.sources, Source(path=str(tmp_path / "nope"))],
        )
        assert generate.collect_passages(corpus, limit=2, seed=0)


class TestCaseShaping:
    def test_every_passage_produces_an_answerable_case(self):
        results = [(passage(), pair(i)) for i in range(4)]
        cases = generate.to_cases(results, refusal_ratio=0.0)
        assert len(cases) == 4
        assert all(not c["expect_refusal"] for c in cases)

    def test_refusal_ratio_controls_the_unanswerable_count(self):
        results = [(passage(), pair(i)) for i in range(10)]
        cases = generate.to_cases(results, refusal_ratio=0.3)
        assert sum(1 for c in cases if c["expect_refusal"]) == 3

    def test_refusal_cases_carry_no_facts_to_score(self):
        cases = generate.to_cases([(passage(), pair())], refusal_ratio=1.0)
        refusals = [c for c in cases if c["expect_refusal"]]
        assert refusals and all(c["expect_facts"] == [] for c in refusals)

    def test_cases_record_where_they_came_from(self):
        cases = generate.to_cases([(passage(source="runbook.md"), pair())], 1.0)
        assert "runbook.md" in cases[0]["notes"]

    def test_ids_are_unique(self):
        cases = generate.to_cases([(passage(), pair(i)) for i in range(5)], refusal_ratio=1.0)
        ids = [c["id"] for c in cases]
        assert len(ids) == len(set(ids))

    def test_output_loads_through_the_eval_harness(self, tmp_path):
        """Generated cases must be readable by evals.run without adaptation."""
        import json

        from evals.run import load_dataset

        cases = generate.to_cases([(passage(), pair(i)) for i in range(3)], 0.5)
        path = tmp_path / "generated.jsonl"
        path.write_text("\n".join(json.dumps(c) for c in cases) + "\n", encoding="utf-8")

        loaded = load_dataset(path)
        assert len(loaded) == len(cases)
        assert any(c.expect_refusal for c in loaded)
        assert any(c.needs_judge for c in loaded)


class TestVerification:
    """A question invented as unanswerable may be answered by another document."""

    @staticmethod
    def cases():
        return [
            {"id": "gen-001", "question": "answerable", "expect_refusal": False},
            {"id": "gen-001-nf", "question": "supposedly not", "expect_refusal": True},
        ]

    def test_drops_refusal_cases_the_corpus_can_answer(self, monkeypatch):
        monkeypatch.setattr(
            "aiplat.knowledge.retrieve",
            lambda q, top_k=3: [{"score": 0.9, "source": "other.md", "text": "..."}],
        )
        kept = __import__("asyncio").run(generate.verify_unanswerable(self.cases()))
        assert [c["id"] for c in kept] == ["gen-001"]

    def test_keeps_refusal_cases_nothing_answers(self, monkeypatch):
        monkeypatch.setattr("aiplat.knowledge.retrieve", lambda q, top_k=3: [])
        kept = __import__("asyncio").run(generate.verify_unanswerable(self.cases()))
        assert len(kept) == 2

    def test_low_scoring_hits_do_not_count_as_answers(self, monkeypatch):
        monkeypatch.setattr(
            "aiplat.knowledge.retrieve",
            lambda q, top_k=3: [{"score": 0.1, "source": "x.md", "text": "..."}],
        )
        kept = __import__("asyncio").run(generate.verify_unanswerable(self.cases()))
        assert len(kept) == 2

    def test_retrieval_failure_keeps_the_case(self, monkeypatch):
        """Losing a refusal case to a transient error would quietly weaken the suite."""

        def boom(q, top_k=3):
            raise RuntimeError("throttled")

        monkeypatch.setattr("aiplat.knowledge.retrieve", boom)
        kept = __import__("asyncio").run(generate.verify_unanswerable(self.cases()))
        assert len(kept) == 2
