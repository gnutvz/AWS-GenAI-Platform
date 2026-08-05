"""Describing a figure with the platform's own model.

This runs once per figure across a whole corpus, unattended, on documents nobody
has inspected. So the interesting cases are not the happy path — they are the
ways a single bad figure could poison a run: an image format Bedrock will not
take, a model call that throws, and a decorative logo that would otherwise get a
confident paragraph written about it and embedded as if it were content.
"""

from __future__ import annotations

import base64

import pytest
from prismdoc.stages.figures import Figure

from services.ingest.figures import EMPTY_MARKER, BedrockFigureProcessor, _bedrock_format


def figure(mime: str = "image/png", fig_id: str = "fig_0_0") -> Figure:
    return Figure(
        id=fig_id,
        page_index=0,
        width=240,
        height=180,
        image_b64=base64.b64encode(b"not-really-an-image").decode("ascii"),
        mime=mime,
    )


@pytest.fixture
def describes(monkeypatch):
    """Replace the model call with a canned answer, and record what it received."""

    def install(answer: str | Exception):
        calls: list[Figure] = []

        def fake(self, fig, image_format):
            calls.append(fig)
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(BedrockFigureProcessor, "_describe", fake)
        return calls

    return install


class TestDescription:
    def test_description_is_labelled_as_coming_from_a_figure(self, describes):
        describes("Network diagram: the API gateway routes to two Lambda functions.")

        text = BedrockFigureProcessor().process(figure())

        assert "Network diagram" in text
        # A reader who retrieves this passage should be able to tell a model read
        # it off a picture, rather than it being the document's own prose.
        assert text.startswith("**Figure fig_0_0:**")

    def test_the_figure_is_what_gets_sent(self, describes):
        calls = describes("something")
        target = figure(fig_id="fig_3_1")

        BedrockFigureProcessor().process(target)

        assert [f.id for f in calls] == ["fig_3_1"]


class TestNothingWorthIndexing:
    def test_a_decorative_figure_contributes_nothing(self, describes):
        """A logo described as 'the company logo' is noise in a retrieval index."""
        describes(EMPTY_MARKER)

        assert BedrockFigureProcessor().process(figure()) == ""

    def test_the_marker_is_honoured_even_inside_a_sentence(self, describes):
        describes("NO INFORMATION - this is a decorative divider.")

        assert BedrockFigureProcessor().process(figure()) == ""

    def test_an_empty_answer_is_not_merged(self, describes):
        describes("   ")

        assert BedrockFigureProcessor().process(figure()) == ""


class TestOneBadFigureCostsOneFigure:
    def test_a_failing_model_call_does_not_raise(self, describes):
        """Ingestion is unattended; a throttle here must not fail the document."""
        describes(RuntimeError("ThrottlingException"))

        assert BedrockFigureProcessor().process(figure()) == ""

    def test_an_unsupported_format_is_skipped_without_calling_the_model(self, describes):
        calls = describes("should not be reached")

        assert BedrockFigureProcessor().process(figure(mime="image/tiff")) == ""
        assert calls == [], "a model call was spent on an image Bedrock cannot read"


class TestFormatMapping:
    @pytest.mark.parametrize(
        ("mime", "expected"),
        [
            ("image/png", "png"),
            ("image/jpeg", "jpeg"),
            ("image/jpg", "jpeg"),  # prismdoc emits this spelling for JPEG sources
            ("image/gif", "gif"),
            ("image/webp", "webp"),
        ],
    )
    def test_supported_types_map_to_bedrock_names(self, mime, expected):
        assert _bedrock_format(mime) == expected

    @pytest.mark.parametrize("mime", ["image/tiff", "image/bmp", "application/pdf", ""])
    def test_unsupported_types_are_rejected_rather_than_guessed(self, mime):
        assert _bedrock_format(mime) is None
