"""Unit tests that need no AWS credentials.

Scope is deliberate: configuration parsing and scoring logic are where a silent bug
does the most damage — a misread env var routes traffic to the wrong place, and a
broken scorer makes every eval result a lie. Anything requiring Bedrock belongs in
the eval suite, not here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiplat import config
from aiplat.knowledge import _source_of
from evals.run import _keyword_recall, _looks_like_refusal, load_dataset


@pytest.fixture(autouse=True)
def clear_settings_cache():
    config.settings.cache_clear()
    yield
    config.settings.cache_clear()


class TestSettings:
    def test_defaults_are_safe(self, monkeypatch):
        for var in ("LLM_ROUTE", "KNOWLEDGE_BASE_ID", "GUARDRAIL_ID", "OTEL_EXPORTER_OTLP_ENDPOINT"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("AWS_REGION", "us-west-2")

        cfg = config.settings()
        assert cfg.llm_route == "bedrock"
        # Optional features are off, not half-configured.
        assert not cfg.retrieval_enabled
        assert not cfg.guardrail_enabled
        assert not cfg.tracing_enabled

    def test_empty_string_counts_as_unset(self, monkeypatch):
        """CDK passes "" for unconfigured values; that must not look like a real ID."""
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.setenv("KNOWLEDGE_BASE_ID", "")
        monkeypatch.setenv("GUARDRAIL_ID", "   ")

        cfg = config.settings()
        assert cfg.knowledge_base_id is None
        assert not cfg.retrieval_enabled
        assert cfg.guardrail_id is None

    def test_invalid_route_fails_loudly(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.setenv("LLM_ROUTE", "openai")
        with pytest.raises(RuntimeError, match="LLM_ROUTE"):
            config.settings()

    def test_require_names_the_missing_setting(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        monkeypatch.delenv("KNOWLEDGE_BASE_ID", raising=False)

        cfg = config.settings()
        with pytest.raises(RuntimeError, match="knowledge_base_id"):
            cfg.require("knowledge_base_id")


class TestCitations:
    def test_prefers_s3_uri(self):
        item = {"location": {"s3Location": {"uri": "s3://bucket/doc.md"}}, "documentId": "abc"}
        assert _source_of(item) == "s3://bucket/doc.md"

    def test_falls_back_to_document_id(self):
        assert _source_of({"location": {}, "documentId": "doc-42"}) == "doc-42"

    def test_never_raises_on_unknown_shape(self):
        assert _source_of({}) == "unknown"


class TestScoring:
    def test_recall_is_case_insensitive(self):
        assert _keyword_recall("The Warranty is 24 MONTHS", ["warranty", "24 months"]) == 1.0

    def test_partial_recall(self):
        assert _keyword_recall("only warranty here", ["warranty", "24 months"]) == 0.5

    def test_no_keywords_means_no_constraint(self):
        assert _keyword_recall("anything at all", []) == 1.0

    @pytest.mark.parametrize(
        "answer",
        ["I don't know.", "No relevant passages found.", "I cannot answer that."],
    )
    def test_detects_refusals(self, answer):
        assert _looks_like_refusal(answer)

    def test_confident_answer_is_not_a_refusal(self):
        assert not _looks_like_refusal("The warranty period is 24 months [1].")


class TestDataset:
    def test_loads_shipped_smoke_dataset(self):
        cases = load_dataset(Path("evals/datasets/smoke.jsonl"))
        assert len(cases) >= 3
        assert any(c.expect_refusal for c in cases), "dataset must cover unanswerable questions"

    def test_rejects_malformed_case(self, tmp_path):
        bad = tmp_path / "bad.jsonl"
        bad.write_text('{"id": "x", "question": "y"\n', encoding="utf-8")
        with pytest.raises(SystemExit, match="not a valid case"):
            load_dataset(bad)

    def test_skips_comments_and_blank_lines(self, tmp_path):
        path = tmp_path / "cases.jsonl"
        path.write_text(
            "# a comment\n\n" + json.dumps({"id": "a", "question": "q?"}) + "\n",
            encoding="utf-8",
        )
        assert len(load_dataset(path)) == 1
