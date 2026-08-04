"""The model seam, including the safety hole that used to sit inside it.

`litellm` is an optional extra, so — like `bedrock-agentcore` — nothing in CI
imports the gateway path. That is how `_gateway_model` came to ignore the
guardrail settings entirely: with `LLM_ROUTE=gateway` and `GUARDRAIL_ID` both
set, the agent ran with no guardrail and logged nothing about it.

Worth being precise about why that is worse than having no guardrail at all. The
guardrail is the compliance artifact — a versioned policy in its own CDK stack,
built to be shown to an auditor. It passing review while never being called is a
failure that looks exactly like success from every angle except the model's.

So both providers are stubbed rather than constructed, and the check runs before
the lazy import, which means the refusal path is testable without the extra
installed.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

from aiplat import config, llm


@pytest.fixture(autouse=True)
def clean_settings(monkeypatch):
    """A bare environment. Each test opts into exactly the settings it needs."""
    for name in (
        "LLM_ROUTE",
        "MODEL_ID",
        "GUARDRAIL_ID",
        "GUARDRAIL_VERSION",
        "GATEWAY_BASE_URL",
        "GATEWAY_API_KEY",
        "GATEWAY_ALLOW_UNGUARDED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    config.settings.cache_clear()
    yield
    config.settings.cache_clear()


class Recorder:
    """Stands in for a model class, remembering the kwargs it was built with."""

    def __init__(self):
        self.kwargs: dict | None = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self


@pytest.fixture
def bedrock(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(llm, "BedrockModel", recorder)
    return recorder


@pytest.fixture
def litellm(monkeypatch):
    """Stub `strands.models.litellm`, which the gateway extra would provide."""
    recorder = Recorder()
    module = types.ModuleType("strands.models.litellm")
    module.LiteLLMModel = recorder
    monkeypatch.setitem(sys.modules, "strands.models.litellm", module)
    return recorder


def use_gateway(monkeypatch, *, guardrail: str | None = None, allow: str | None = None):
    monkeypatch.setenv("LLM_ROUTE", "gateway")
    monkeypatch.setenv("GATEWAY_BASE_URL", "http://localhost:4000")
    if guardrail:
        monkeypatch.setenv("GUARDRAIL_ID", guardrail)
    if allow:
        monkeypatch.setenv("GATEWAY_ALLOW_UNGUARDED", allow)
    config.settings.cache_clear()


class TestGuardrailCoverage:
    def test_gateway_with_a_guardrail_configured_refuses_to_start(self, monkeypatch):
        """The combination that used to run unguarded and silent."""
        use_gateway(monkeypatch, guardrail="gr-abc123")

        with pytest.raises(RuntimeError) as exc:
            llm.build_model()

        message = str(exc.value)
        assert "gr-abc123" in message, "the operator should not have to go looking for it"
        # The error is only useful if it says what to do instead.
        assert "LLM_ROUTE=bedrock" in message
        assert "GATEWAY_ALLOW_UNGUARDED" in message

    def test_gateway_without_a_guardrail_is_fine(self, monkeypatch, litellm):
        """No guardrail configured is a choice, not an accident."""
        use_gateway(monkeypatch)
        assert llm.build_model() is litellm

    def test_explicit_opt_in_is_allowed_but_logged(self, monkeypatch, litellm, caplog):
        use_gateway(monkeypatch, guardrail="gr-abc123", allow="true")

        with caplog.at_level(logging.WARNING, logger="aiplat.llm"):
            assert llm.build_model() is litellm

        assert "gr-abc123" in caplog.text
        assert "NOT" in caplog.text, "an opt-out of enforcement must be visible in the log"

    @pytest.mark.parametrize("value", ["false", "no", "0", "", "maybe"])
    def test_only_an_explicit_yes_counts_as_opting_in(self, monkeypatch, value):
        """A typo in the flag must fail closed, not open."""
        use_gateway(monkeypatch, guardrail="gr-abc123", allow=value)
        with pytest.raises(RuntimeError):
            llm.build_model()

    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
    def test_the_usual_spellings_of_yes_all_work(self, monkeypatch, litellm, value):
        use_gateway(monkeypatch, guardrail="gr-abc123", allow=value)
        assert llm.build_model() is litellm


class TestBedrockRoute:
    def test_guardrail_is_attached_when_configured(self, monkeypatch, bedrock):
        monkeypatch.setenv("GUARDRAIL_ID", "gr-abc123")
        monkeypatch.setenv("GUARDRAIL_VERSION", "3")
        config.settings.cache_clear()

        llm.build_model()

        assert bedrock.kwargs["guardrail_id"] == "gr-abc123"
        assert bedrock.kwargs["guardrail_version"] == "3"
        assert bedrock.kwargs["guardrail_trace"] == "enabled"

    def test_no_guardrail_settings_when_none_is_configured(self, monkeypatch, bedrock):
        llm.build_model()
        assert "guardrail_id" not in bedrock.kwargs

    def test_prompt_caching_is_on(self, monkeypatch, bedrock):
        llm.build_model()
        assert bedrock.kwargs["cache_prompt"] == "default"

    def test_the_unguarded_flag_does_not_touch_this_route(self, monkeypatch, bedrock):
        """The opt-out is about the gateway; it must not weaken the default path."""
        monkeypatch.setenv("GUARDRAIL_ID", "gr-abc123")
        monkeypatch.setenv("GATEWAY_ALLOW_UNGUARDED", "true")
        config.settings.cache_clear()

        llm.build_model()

        assert bedrock.kwargs["guardrail_id"] == "gr-abc123"

    def test_overrides_win_over_platform_defaults(self, monkeypatch, bedrock):
        llm.build_model({"temperature": 0.2, "cache_prompt": None})
        assert bedrock.kwargs["temperature"] == 0.2
        assert bedrock.kwargs["cache_prompt"] is None
