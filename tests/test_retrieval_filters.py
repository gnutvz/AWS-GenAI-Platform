"""Filtered retrieval, and the boundary that makes it worth anything.

A retrieval filter is only a security control if the thing being restricted
cannot remove it. The model is a caller like any other — and unlike the others,
it reads attacker-influenced text on every turn, since a retrieved passage can
contain instructions. A `filters` argument on the tool would be a filter the
model can drop, and the tool schema would helpfully document how.

So these tests check two different things. That filters reach Bedrock in the
right shape, which is ordinary correctness. And that the tool the model sees has
no parameter for them, which is the part that stops being true the moment
someone adds a convenience argument.
"""

from __future__ import annotations

import boto3
import pytest
from botocore.stub import ANY, Stubber

from aiplat import config, knowledge
from services.agent.agent import build_agent


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("KNOWLEDGE_BASE_ID", "kb-test-123")
    _reset()
    yield
    _reset()


def _reset() -> None:
    config.settings.cache_clear()
    clear = getattr(knowledge._client, "cache_clear", None)
    if clear:
        clear()


@pytest.fixture
def stubbed_kb(monkeypatch):
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


def expect_retrieve(stubber, vector_search: dict) -> None:
    stubber.add_response(
        "retrieve",
        {"retrievalResults": []},
        {
            "knowledgeBaseId": "kb-test-123",
            "retrievalQuery": {"text": ANY},
            "retrievalConfiguration": {"vectorSearchConfiguration": vector_search},
        },
    )


BASE = {"numberOfResults": 6, "overrideSearchType": "HYBRID"}


class TestFilterShape:
    def test_one_attribute_needs_no_conjunction(self):
        assert knowledge.as_bedrock_filter({"doc_type": "technical"}) == {
            "equals": {"key": "doc_type", "value": "technical"}
        }

    def test_several_attributes_are_all_required(self):
        """andAll, never orAll — a filter that widens is not a restriction."""
        built = knowledge.as_bedrock_filter({"doc_type": "technical", "department": "eng"})
        assert set(built) == {"andAll"}
        assert built["andAll"] == [
            {"equals": {"key": "department", "value": "eng"}},
            {"equals": {"key": "doc_type", "value": "technical"}},
        ]

    def test_ordering_is_stable(self):
        """Same filters, same request — otherwise nothing downstream can cache."""
        a = knowledge.as_bedrock_filter({"b": "2", "a": "1"})
        b = knowledge.as_bedrock_filter({"a": "1", "b": "2"})
        assert a == b


class TestFiltersReachBedrock:
    def test_no_filter_key_when_none_are_given(self, stubbed_kb):
        """An empty filter must be absent, not an empty clause Bedrock may reject."""
        expect_retrieve(stubbed_kb, BASE)
        knowledge.retrieve("query")
        stubbed_kb.assert_no_pending_responses()

    def test_filters_are_sent(self, stubbed_kb):
        expect_retrieve(
            stubbed_kb,
            {**BASE, "filter": {"equals": {"key": "doc_type", "value": "technical"}}},
        )
        knowledge.retrieve("query", filters={"doc_type": "technical"})
        stubbed_kb.assert_no_pending_responses()

    def test_an_empty_dict_is_treated_as_no_filter(self, stubbed_kb):
        expect_retrieve(stubbed_kb, BASE)
        knowledge.retrieve("query", filters={})
        stubbed_kb.assert_no_pending_responses()

    def test_the_tool_applies_the_filters_it_was_built_with(self, stubbed_kb):
        expect_retrieve(
            stubbed_kb,
            {**BASE, "filter": {"equals": {"key": "department", "value": "legal"}}},
        )
        knowledge.make_search_tool({"department": "legal"})("query")
        stubbed_kb.assert_no_pending_responses()


class TestModelCannotWidenTheFilter:
    """The invariant. Everything above is correctness; this is the control."""

    def test_the_tool_exposes_no_filter_parameter(self):
        """What the model sees. A convenience argument here undoes the whole thing."""
        spec = knowledge.make_search_tool({"department": "legal"}).tool_spec
        properties = spec["inputSchema"]["json"]["properties"]

        assert set(properties) <= {"query", "top_k"}, (
            f"the retrieval tool exposes {sorted(properties)} to the model. A filter the "
            f"model can name is a filter it can change — and a passage it retrieves can "
            f"tell it to."
        )

    def test_filters_are_not_described_to_the_model(self):
        """Not in the schema is not enough if the description explains them."""
        spec = knowledge.make_search_tool({"department": "legal"}).tool_spec
        assert "legal" not in str(spec), "the filter value leaked into the tool schema"

    def test_two_tools_do_not_share_filters(self, stubbed_kb):
        """Each closure holds its own, so building a second cannot loosen the first."""
        restricted = knowledge.make_search_tool({"department": "legal"})
        knowledge.make_search_tool({"department": "eng"})

        expect_retrieve(
            stubbed_kb,
            {**BASE, "filter": {"equals": {"key": "department", "value": "legal"}}},
        )
        restricted("query")
        stubbed_kb.assert_no_pending_responses()

    def test_the_default_tool_is_unfiltered(self, stubbed_kb):
        """No filters means no filters — not a stale set from another caller."""
        expect_retrieve(stubbed_kb, BASE)
        knowledge.search_knowledge_base("query")
        stubbed_kb.assert_no_pending_responses()


class TestAgentWiring:
    def test_build_agent_passes_filters_through_to_the_tool(self, stubbed_kb, monkeypatch):
        monkeypatch.setattr("services.agent.agent.build_model", lambda: None)

        agent = build_agent(retrieval_filters={"department": "legal"})
        tool = next(iter(agent.tool_registry.registry.values()))

        expect_retrieve(
            stubbed_kb,
            {**BASE, "filter": {"equals": {"key": "department", "value": "legal"}}},
        )
        tool("query")
        stubbed_kb.assert_no_pending_responses()
