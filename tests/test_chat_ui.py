"""Chat UI helpers.

Only the pure functions are tested — the Chainlit decorators need a running
server and a websocket, which is not something a unit test should stand up. What
is worth pinning down is the parsing that turns retrieval output into what a
viewer sees: get that wrong on a demo and the sources panel is silently empty
while the answer still looks fine.
"""

from __future__ import annotations

import pytest

from app import chat


def tool_output(*passages: tuple[int, float, str]) -> str:
    return "\n\n".join(
        f"[{n}] (score {score}, source: {source})\nPassage body here."
        for n, score, source in passages
    )


class TestPassageSummary:
    def test_lists_every_passage_with_its_source(self):
        headline, sources = chat.summarise_passages(
            tool_output((1, 0.82, "s3://bucket/runbook.md"), (2, 0.61, "s3://bucket/policy.md"))
        )
        assert headline == "Found 2 passage(s)"
        assert "runbook.md" in sources[0]
        assert "0.82" in sources[0]

    def test_empty_retrieval_says_so_rather_than_showing_nothing(self):
        """A blank step looks like a bug; "no passages" is information."""
        headline, sources = chat.summarise_passages(
            "No relevant passages found. Say so rather than guessing."
        )
        assert headline == "No relevant passages found"
        assert sources == []

    def test_tolerates_a_failed_retrieval_message(self):
        headline, sources = chat.summarise_passages("Retrieval failed: ThrottlingException")
        assert sources == []
        assert headline

    @pytest.mark.parametrize("value", ["", None])
    def test_missing_output_does_not_raise(self, value):
        assert chat.summarise_passages(value) == ("No relevant passages found", [])


class TestRefusalDetection:
    @pytest.mark.parametrize(
        "answer",
        [
            "I don't know based on the available sources.",
            "I do not know.",
            "No relevant passages were found for that.",
            "I cannot answer that from the documentation.",
        ],
    )
    def test_recognises_a_refusal(self, answer):
        assert chat._looks_like_refusal(answer)

    def test_a_cited_answer_is_not_a_refusal(self):
        assert not chat._looks_like_refusal("The warranty period is 24 months [1].")

    def test_empty_answer_is_not_treated_as_a_refusal(self):
        assert not chat._looks_like_refusal("")


class TestToolLabels:
    def test_retrieval_gets_a_human_label(self):
        """Viewers see "Searching the knowledge base", not a function name."""
        assert chat._label("search_knowledge_base") == "Searching the knowledge base"

    def test_unknown_tools_fall_back_to_their_name(self):
        assert chat._label("some_future_tool") == "some_future_tool"
