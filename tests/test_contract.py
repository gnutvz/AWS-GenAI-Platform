"""The response contract — the platform's actual product surface.

Any UI can sit in front of this platform, which is the point: Chainlit is one
example client, swappable for Slack, Teams, a widget or someone else's frontend.
That interchangeability is exactly what makes the response shape load-bearing.
It is the only thing every client agrees on, and until now it existed nowhere
but as a dict literal in `agent.ask()`.

Renaming a key there is a silent break. Nothing fails to build, no test objects,
and every deployed client stops finding the field — the failure surfaces as an
empty answer pane, not as an error. So this file pins the wire format itself,
separately from `test_services.py`, which covers handler mechanics (event
shapes, error redaction) rather than the payload clients read.

Adding a field is safe and needs no change here. Removing or renaming one should
have to argue with a test first.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import types

import pytest

from services.agent import agent as agent_module
from services.agent import lambda_handler

# Every key a client may rely on. Growing this list is a feature; shrinking it,
# or renaming anything in it, breaks every deployed client at once.
ANSWER_FIELDS = {"answer", "stop_reason", "session_id", "tenant", "usage"}
USAGE_FIELDS = {"input_tokens", "output_tokens", "total_tokens"}


class FakeResult:
    """What Strands hands back from invoke_async, reduced to what ask() reads."""

    def __init__(self, text="An answer.", stop_reason="end_turn", usage=None):
        self.text = text
        self.stop_reason = stop_reason
        self.metrics = types.SimpleNamespace(
            accumulated_usage=usage
            if usage is not None
            else {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}
        )

    def __str__(self) -> str:
        return self.text


@pytest.fixture
def stub_agent(monkeypatch):
    """Replace the agent with something that answers without calling Bedrock."""

    def install(result=None):
        class FakeAgent:
            async def invoke_async(self, prompt):
                return result or FakeResult()

        monkeypatch.setattr(agent_module, "build_agent", lambda **kwargs: FakeAgent())

    return install


@pytest.fixture(autouse=True)
def tenant_env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("TENANT", "acme")
    agent_module.settings.cache_clear()
    yield
    agent_module.settings.cache_clear()


def ask(**kwargs) -> dict:
    return asyncio.run(agent_module.ask(**kwargs))


class TestAnswerShape:
    def test_exactly_the_documented_fields(self, stub_agent):
        stub_agent()
        assert set(ask(prompt="hi")) == ANSWER_FIELDS

    def test_usage_reports_all_three_counts(self, stub_agent):
        stub_agent()
        assert set(ask(prompt="hi")["usage"]) == USAGE_FIELDS

    def test_usage_keys_are_present_even_when_the_model_reports_nothing(self, stub_agent):
        """A missing count must read as null, not as an absent key."""
        stub_agent(FakeResult(usage={}))
        usage = ask(prompt="hi")["usage"]
        assert set(usage) == USAGE_FIELDS
        assert all(value is None for value in usage.values())

    def test_tenant_comes_from_the_environment(self, stub_agent):
        """Clients are told which tenant answered; the caller does not choose it."""
        stub_agent()
        assert ask(prompt="hi")["tenant"] == "acme"

    def test_session_id_round_trips(self, stub_agent):
        stub_agent()
        assert ask(prompt="hi", session_id="s-1")["session_id"] == "s-1"

    def test_stop_reason_is_passed_through(self, stub_agent):
        stub_agent(FakeResult(stop_reason="max_tokens"))
        assert ask(prompt="hi")["stop_reason"] == "max_tokens"

    def test_the_whole_payload_is_json_serialisable(self, stub_agent):
        """It is about to be a JSON body; a stray object fails at the boundary."""
        stub_agent()
        json.dumps(ask(prompt="hi"))


class TestHttpEnvelope:
    @staticmethod
    def _stub_handler_ask(monkeypatch, payload):
        async def fake_ask(prompt, session_id=None):
            return payload

        monkeypatch.setattr(lambda_handler, "ask", fake_ask)

    def test_success_body_is_the_answer_verbatim(self, monkeypatch):
        """The envelope must not reshape, wrap or rename what ask() produced."""
        payload = {
            "answer": "24 months.",
            "stop_reason": "end_turn",
            "session_id": None,
            "tenant": "acme",
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        }
        self._stub_handler_ask(monkeypatch, payload)

        response = lambda_handler.handler({"prompt": "warranty?"}, None)
        assert json.loads(response["body"]) == payload

    def test_content_type_is_json(self, monkeypatch):
        self._stub_handler_ask(monkeypatch, {"answer": "x"})
        response = lambda_handler.handler({"prompt": "hi"}, None)
        assert response["headers"]["Content-Type"] == "application/json"

    def test_error_bodies_have_a_stable_shape(self, monkeypatch):
        """Clients branch on this too, so it is as much a contract as success."""
        self._stub_handler_ask(monkeypatch, {})
        assert set(json.loads(lambda_handler.handler({}, None)["body"])) == {"error"}

    def test_non_ascii_survives_the_envelope(self, monkeypatch):
        self._stub_handler_ask(monkeypatch, {"answer": "Bảo hành 24 tháng"})
        response = lambda_handler.handler({"prompt": "bảo hành?"}, None)
        assert json.loads(response["body"])["answer"] == "Bảo hành 24 tháng"


class TestStubsMatchReality:
    def test_the_handler_calls_ask_the_way_ask_is_defined(self):
        """A stub looser than the real function hides the call it should catch.

        test_services.py stubbed `ask` with a `tenant` parameter that `ask` has
        never had — the same wrong mental model that shipped a caller-supplied
        tenant in the AgentCore entrypoint.
        """
        params = set(inspect.signature(agent_module.ask).parameters)
        assert params == {"prompt", "session_id"}, (
            "ask() changed signature; the handler and every test stub of it need to "
            "change with it"
        )
