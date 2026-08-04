"""Retry and timeout policy for every AWS call the platform makes.

Bedrock throttles on tokens per minute, which makes rate limiting the normal
condition of a busy tenant rather than an incident. boto3's default is two
attempts with no client-side pacing, so the default behaviour was to turn a
quota the account was always going to hit into an error the user sees.

These tests pin the policy where it is applied, not just where it is defined —
a config object nobody passes to a client is the same as no config at all.
"""

from __future__ import annotations

import boto3
import pytest

from aiplat import config, knowledge, llm
from aiplat.aws import MAX_ATTEMPTS, boto_config
from services.ingest import ingest


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.delenv("GUARDRAIL_ID", raising=False)
    monkeypatch.delenv("LLM_ROUTE", raising=False)
    _clear_caches()
    yield
    _clear_caches()


def _clear_caches() -> None:
    config.settings.cache_clear()
    for cached in (knowledge._client, ingest._s3, ingest._bedrock_agent):
        clear = getattr(cached, "cache_clear", None)
        if clear:
            clear()


class TestPolicy:
    def test_uses_adaptive_retries(self):
        """Standard mode retries one request; adaptive slows the whole client."""
        retries = boto_config().retries
        assert retries["mode"] == "adaptive"
        assert retries["max_attempts"] == MAX_ATTEMPTS

    def test_sets_both_timeouts(self):
        cfg = boto_config()
        assert cfg.connect_timeout == 5
        assert cfg.read_timeout == 45

    def test_overrides_win(self):
        assert boto_config(read_timeout=20).read_timeout == 20

    def test_overriding_one_field_keeps_the_rest(self):
        cfg = boto_config(read_timeout=20)
        assert cfg.retries["mode"] == "adaptive"
        assert cfg.connect_timeout == 5

    def test_worst_case_fits_inside_the_lambda_timeout(self):
        """The numbers are only defensible against the budget they sit in."""
        lambda_timeout = 300  # infra/stacks/api_stack.py
        model = MAX_ATTEMPTS * boto_config().read_timeout
        retrieval = MAX_ATTEMPTS * boto_config(read_timeout=20).read_timeout
        assert model + retrieval < lambda_timeout


class TestAppliedToClients:
    def test_retrieval_client_carries_the_policy(self):
        cfg = knowledge._client().meta.config
        assert cfg.retries["mode"] == "adaptive"
        assert cfg.read_timeout == 20, "retrieval holds a user request open while it waits"

    def test_model_is_built_with_the_policy(self, monkeypatch):
        captured = {}

        def fake_bedrock(**kwargs):
            captured.update(kwargs)

        monkeypatch.setattr(llm, "BedrockModel", fake_bedrock)
        llm.build_model()

        assert "boto_client_config" in captured, (
            "the model call is the one most likely to be throttled — a retry policy "
            "that skips it is the policy not being applied where it matters"
        )
        assert captured["boto_client_config"].retries["mode"] == "adaptive"

    def test_ingest_clients_carry_the_policy(self):
        assert ingest._s3().meta.config.retries["mode"] == "adaptive"
        assert ingest._bedrock_agent().meta.config.retries["mode"] == "adaptive"


class TestClientReuse:
    def test_upload_does_not_build_a_client_per_document(self, monkeypatch):
        """upload() runs once per file; a 5,000-document corpus noticed."""
        built = []
        real = boto3.client

        def counting_client(service, **kwargs):
            built.append(service)
            return real(service, **kwargs)

        monkeypatch.setattr(ingest.boto3, "client", counting_client)

        assert ingest._s3() is ingest._s3()
        assert built.count("s3") == 1
