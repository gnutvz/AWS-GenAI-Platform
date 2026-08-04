"""Service tests with AWS stubbed out.

These cover the code paths that only run against a deployed stack — retrieval
filtering, the Lambda request/response contract, request signing. Stubbing means
they run in CI and on a laptop with no credentials, which is the point: the
parts most likely to break in production shouldn't be the parts only production
can test.

What is deliberately NOT here: whether Bedrock returns good passages. That is
what the eval suite is for.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import boto3
import pytest
from botocore.stub import ANY, Stubber

from aiplat import config, knowledge
from services.agent import lambda_handler

# scripts/ holds operator tooling, not an importable package — it is deliberately
# not part of the wheel, so tests reach it by path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import write_env


@pytest.fixture(autouse=True)
def configured_env(monkeypatch):
    """Every test runs against a fully configured platform unless it says otherwise."""
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("KNOWLEDGE_BASE_ID", "kb-test-123")
    _reset_caches()
    yield
    _reset_caches()


def _reset_caches() -> None:
    config.settings.cache_clear()
    # _client may currently be a monkeypatched stand-in with no cache — teardown
    # ordering means this can run before monkeypatch restores the real function.
    clear = getattr(knowledge._client, "cache_clear", None)
    if clear:
        clear()


@pytest.fixture
def stubbed_kb(monkeypatch):
    """A bedrock-agent-runtime client with canned responses."""
    client = boto3.client(
        "bedrock-agent-runtime",
        region_name="us-west-2",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    stubber = Stubber(client)
    monkeypatch.setattr(knowledge, "_client", lambda: client)
    with stubber:
        yield stubber


def _passage(text: str, score: float, uri: str = "s3://bucket/doc.md") -> dict:
    return {
        "content": {"text": text},
        "score": score,
        "location": {"type": "S3", "s3Location": {"uri": uri}},
    }


class TestRetrieve:
    def test_returns_passages_above_threshold(self, stubbed_kb):
        stubbed_kb.add_response(
            "retrieve",
            {"retrievalResults": [_passage("The warranty is 24 months.", 0.82)]},
            {
                "knowledgeBaseId": "kb-test-123",
                "retrievalQuery": {"text": "warranty"},
                "retrievalConfiguration": ANY,
            },
        )

        passages = knowledge.retrieve("warranty")
        assert len(passages) == 1
        assert passages[0]["text"] == "The warranty is 24 months."
        assert passages[0]["source"] == "s3://bucket/doc.md"

    def test_drops_low_scoring_passages(self, stubbed_kb):
        """Below MIN_SCORE the passage is topically near but not answering."""
        stubbed_kb.add_response(
            "retrieve",
            {
                "retrievalResults": [
                    _passage("relevant", 0.9),
                    _passage("tangential", 0.1),
                ]
            },
            {"knowledgeBaseId": ANY, "retrievalQuery": ANY, "retrievalConfiguration": ANY},
        )

        passages = knowledge.retrieve("anything")
        assert [p["text"] for p in passages] == ["relevant"]

    def test_requests_hybrid_search(self, stubbed_kb):
        """Pure semantic search misses exact identifiers — hybrid is not optional.

        The Stubber raises if the call doesn't match these params exactly, so this
        asserts the real request shape rather than a locally-built copy of it.
        """
        stubbed_kb.add_response(
            "retrieve",
            {"retrievalResults": []},
            {
                "knowledgeBaseId": "kb-test-123",
                "retrievalQuery": {"text": "error code E42"},
                "retrievalConfiguration": {
                    "vectorSearchConfiguration": {
                        "numberOfResults": 3,
                        "overrideSearchType": "HYBRID",
                    }
                },
            },
        )

        assert knowledge.retrieve("error code E42", top_k=3) == []
        stubbed_kb.assert_no_pending_responses()

    def test_fails_loudly_without_knowledge_base(self, monkeypatch):
        monkeypatch.delenv("KNOWLEDGE_BASE_ID", raising=False)
        config.settings.cache_clear()
        with pytest.raises(RuntimeError, match="knowledge_base_id"):
            knowledge.retrieve("anything")


class TestSearchTool:
    """The tool wrapper is what the model sees — its output contract matters."""

    def test_formats_passages_with_citation_markers(self, stubbed_kb):
        stubbed_kb.add_response(
            "retrieve",
            {"retrievalResults": [_passage("Answer text.", 0.9, "s3://b/policy.md")]},
        )
        output = knowledge.search_knowledge_base("query")
        assert "[1]" in output
        assert "s3://b/policy.md" in output
        assert "Answer text." in output

    def test_tells_the_model_not_to_guess_when_empty(self, stubbed_kb):
        stubbed_kb.add_response("retrieve", {"retrievalResults": []})
        output = knowledge.search_knowledge_base("query")
        assert "No relevant passages" in output

    def test_returns_error_to_model_instead_of_raising(self, stubbed_kb):
        """A raised exception kills the turn; a message lets the agent recover."""
        stubbed_kb.add_client_error("retrieve", service_error_code="ThrottlingException")
        output = knowledge.search_knowledge_base("query")
        assert "Retrieval failed" in output


class TestLambdaHandler:
    @staticmethod
    def _stub_ask(monkeypatch, result=None, raises=None):
        # Signature matches ask() exactly — no `tenant`. A stub looser than the
        # real function accepts calls the real one would reject, which is how a
        # caller-supplied tenant survived review in the AgentCore entrypoint.
        # tests/test_contract.py asserts the two stay in step.
        async def fake_ask(prompt, session_id=None):
            if raises:
                raise raises
            return result or {
                "answer": f"answered: {prompt}",
                "stop_reason": "end_turn",
                "session_id": session_id,
                "tenant": "default",
                "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            }

        monkeypatch.setattr(lambda_handler, "ask", fake_ask)

    def test_accepts_api_gateway_proxy_event(self, monkeypatch):
        self._stub_ask(monkeypatch)
        response = lambda_handler.handler({"body": json.dumps({"prompt": "hi"})}, None)
        assert response["statusCode"] == 200
        assert json.loads(response["body"])["answer"] == "answered: hi"

    def test_accepts_direct_invocation_payload(self, monkeypatch):
        """Direct `aws lambda invoke` has no "body" wrapper."""
        self._stub_ask(monkeypatch)
        response = lambda_handler.handler({"prompt": "hi"}, None)
        assert response["statusCode"] == 200

    def test_rejects_missing_prompt(self, monkeypatch):
        self._stub_ask(monkeypatch)
        response = lambda_handler.handler({"body": "{}"}, None)
        assert response["statusCode"] == 400
        assert "prompt" in json.loads(response["body"])["error"]

    def test_does_not_leak_stack_traces(self, monkeypatch):
        """Internal detail goes to logs, not to the caller."""
        self._stub_ask(monkeypatch, raises=ValueError("boto3 credentials at /home/user/.aws"))
        response = lambda_handler.handler({"prompt": "hi"}, None)

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body == {"error": "Agent invocation failed", "type": "ValueError"}
        assert "/home/user" not in response["body"]

    def test_response_is_utf8_safe(self, monkeypatch):
        """Vietnamese answers must not come back as \\uXXXX escapes."""
        self._stub_ask(monkeypatch, result={"answer": "Bảo hành 24 tháng", "usage": {}})
        response = lambda_handler.handler({"prompt": "bảo hành?"}, None)
        assert "Bảo hành 24 tháng" in response["body"]


class TestWriteEnv:
    """`make env` rewrites a file the user owns — it must not eat their edits."""

    def test_updates_existing_keys_in_place(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("AWS_REGION=us-west-2\nKNOWLEDGE_BASE_ID=\n", encoding="utf-8")

        updated, added = write_env.merge_into_env(env, {"KNOWLEDGE_BASE_ID": "kb-new"})

        assert (updated, added) == (1, 0)
        assert "KNOWLEDGE_BASE_ID=kb-new" in env.read_text()
        assert "AWS_REGION=us-west-2" in env.read_text()

    def test_preserves_comments_and_unrelated_lines(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text(
            "# my notes\nMY_OWN_VAR=keep-me\nKNOWLEDGE_BASE_ID=old\n", encoding="utf-8"
        )

        write_env.merge_into_env(env, {"KNOWLEDGE_BASE_ID": "kb-new"})

        content = env.read_text()
        assert "# my notes" in content
        assert "MY_OWN_VAR=keep-me" in content
        assert "old" not in content

    def test_appends_keys_that_are_not_there_yet(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("AWS_REGION=us-west-2\n", encoding="utf-8")

        updated, added = write_env.merge_into_env(env, {"AGENT_FUNCTION_URL": "https://x/"})

        assert (updated, added) == (0, 1)
        assert "AGENT_FUNCTION_URL=https://x/" in env.read_text()

    def test_writes_a_new_file_when_none_exists(self, tmp_path):
        env = tmp_path / ".env"
        write_env.merge_into_env(env, {"KNOWLEDGE_BASE_ID": "kb-1"})
        assert env.read_text().strip().endswith("KNOWLEDGE_BASE_ID=kb-1")

    def test_every_mapped_output_is_a_documented_env_key(self):
        """A stack output nobody reads is a silent no-op — catch the drift here."""
        documented = Path(".env.example").read_text(encoding="utf-8")
        for env_key in write_env.OUTPUT_TO_ENV.values():
            assert f"{env_key}=" in documented, f"{env_key} missing from .env.example"
