"""Unit tests that need no AWS credentials.

Scope is deliberate: configuration parsing and scoring logic are where a silent bug
does the most damage — a misread env var routes traffic to the wrong place, and a
broken scorer makes every eval result a lie. Anything requiring Bedrock belongs in
the eval suite, not here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import ClassVar

import pytest

from aiplat import config
from aiplat.knowledge import _source_of
from evals.datasets.fetch_enterprise_bench import to_cases
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


class TestBenchmarkConversion:
    """EnterpriseRAG-Bench -> harness cases."""

    ANSWERABLE: ClassVar[dict] = {
        "question_id": "qst_0001",
        "question_type": "basic",
        "source_types": ["confluence"],
        "question": "What is the limit?",
        "expected_doc_ids": ["dsid_aaa"],
        "gold_answer": "10 MiB.",
        "answer_facts": ["The limit is 10 MiB."],
    }
    # Refusal questions ship with empty source_types AND empty expected_doc_ids.
    UNANSWERABLE: ClassVar[dict] = {
        "question_id": "qst_0500",
        "question_type": "info_not_found",
        "source_types": [],
        "question": "Which accounts were on the allowlist?",
        "expected_doc_ids": [],
        "gold_answer": "Not answerable from available documents.",
        "answer_facts": [],
    }

    def test_keeps_refusal_questions_despite_empty_sources(self):
        """Regression: filtering refusals by source dropped all 20 of them."""
        cases = to_cases([self.UNANSWERABLE], {"confluence"}, {"dsid_aaa"})
        assert len(cases) == 1
        assert cases[0]["expect_refusal"] is True
        assert cases[0]["expect_facts"] == []

    def test_keeps_answerable_question_when_evidence_present(self):
        cases = to_cases([self.ANSWERABLE], {"confluence"}, {"dsid_aaa"})
        assert len(cases) == 1
        assert cases[0]["expect_refusal"] is False
        assert cases[0]["expect_facts"] == ["The limit is 10 MiB."]

    def test_drops_question_whose_evidence_was_not_downloaded(self):
        """Scoring an agent on a document it could never retrieve is meaningless."""
        assert to_cases([self.ANSWERABLE], {"confluence"}, {"dsid_other"}) == []

    def test_output_matches_case_schema(self, tmp_path):
        """Converted cases must load through the harness without adaptation."""
        cases = to_cases([self.ANSWERABLE, self.UNANSWERABLE], {"confluence"}, {"dsid_aaa"})
        path = tmp_path / "converted.jsonl"
        path.write_text(
            "\n".join(json.dumps(c) for c in cases) + "\n",
            encoding="utf-8",
        )
        loaded = load_dataset(path)
        assert len(loaded) == 2
        assert loaded[0].needs_judge is True
        assert loaded[1].expect_refusal is True


class TestBlankMeansAbsent:
    """A variable that exists and is empty must not beat its default.

    The handover template hands someone a file and tells them to leave optional
    fields blank, which is exactly how `MODEL_ID=` comes to exist with no value.
    Before this, that produced an agent built with no model id and a prompt path
    resolving to the prompts *directory* rather than a prompt — both failing far
    from the blank line that caused them.
    """

    @pytest.fixture(autouse=True)
    def clean(self, monkeypatch):
        monkeypatch.setenv("AWS_REGION", "us-west-2")
        config.settings.cache_clear()
        yield
        config.settings.cache_clear()

    def test_blank_model_id_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("MODEL_ID", "")
        config.settings.cache_clear()

        assert config.settings().model_id == config.DEFAULT_MODEL_ID

    def test_blank_prompt_name_falls_back_to_system(self, monkeypatch):
        monkeypatch.setenv("PROMPT_NAME", "")
        config.settings.cache_clear()

        assert config.settings().prompt_name == "system"

    def test_blank_llm_route_falls_back_to_bedrock(self, monkeypatch):
        monkeypatch.setenv("LLM_ROUTE", "")
        config.settings.cache_clear()

        assert config.settings().llm_route == "bedrock"

    def test_whitespace_only_counts_as_blank(self, monkeypatch):
        monkeypatch.setenv("MODEL_ID", "   ")
        config.settings.cache_clear()

        assert config.settings().model_id == config.DEFAULT_MODEL_ID

    def test_a_real_value_still_wins(self, monkeypatch):
        monkeypatch.setenv("MODEL_ID", "some-other-model")
        config.settings.cache_clear()

        assert config.settings().model_id == "some-other-model"


class TestDotenvLoading:
    """`.env` has to reach every entry point, not just `make ask`.

    It was loaded in scripts/ask.py alone, so a machine configured entirely
    through .env could ask the deployed agent and fail at ingest, eval and the
    chat UI — reporting a missing setting rather than a file nobody read.
    """

    def test_values_are_read_from_the_file(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("KNOWLEDGE_BASE_ID=kb-from-file\n", encoding="utf-8")
        monkeypatch.delenv("KNOWLEDGE_BASE_ID", raising=False)

        config.load_dotenv(env)

        assert os.environ["KNOWLEDGE_BASE_ID"] == "kb-from-file"

    def test_a_real_environment_variable_wins(self, tmp_path, monkeypatch):
        """`AWS_PROFILE=other make eval` must do what it looks like it does."""
        env = tmp_path / ".env"
        env.write_text("TENANT=from-file\n", encoding="utf-8")
        monkeypatch.setenv("TENANT", "from-shell")

        config.load_dotenv(env)

        assert os.environ["TENANT"] == "from-shell"

    def test_quotes_are_stripped(self, tmp_path, monkeypatch):
        """Values pasted from a console often arrive wrapped."""
        env = tmp_path / ".env"
        env.write_text('MODEL_ID="global.anthropic.claude-sonnet-5"\n', encoding="utf-8")
        monkeypatch.delenv("MODEL_ID", raising=False)

        config.load_dotenv(env)

        assert os.environ["MODEL_ID"] == "global.anthropic.claude-sonnet-5"

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        """Lambda has no .env, and importing aiplat must not care."""
        config.load_dotenv(tmp_path / "nope.env")

    def test_comments_and_blanks_are_skipped(self, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("# a comment\n\nTENANT=acme\n", encoding="utf-8")
        monkeypatch.delenv("TENANT", raising=False)

        config.load_dotenv(env)

        assert os.environ["TENANT"] == "acme"
