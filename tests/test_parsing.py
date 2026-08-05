"""Document → Markdown routing, and the figure path that closes a silent gap.

The defect this replaced was not a crash. Calling docling with default options
extracted text and tables correctly and dropped every figure on the floor —
docling disables picture description and classification by default, so a diagram
became an empty placeholder. The document logged as ingested, the corpus looked
complete, and the agent simply could not answer questions about anything drawn
rather than written.

So the tests that matter here are about *silence*: that figure text reaches the
Markdown when asked for, that nothing pretends to describe a figure when it is
switched off, and that a figure the model cannot read costs one figure rather
than the document.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiplat import config
from services.ingest import parsing


@pytest.fixture(scope="module")
def real_pdf() -> Path:
    """A committed one-page PDF carrying text and one embedded figure.

    A real file rather than a mock: the behaviour under test is whether a figure
    survives extraction, description and merging into Markdown, and every step of
    that is in library code a stub would replace.
    """
    fixture = Path(__file__).parent / "fixtures" / "page_with_figure.pdf"
    assert fixture.exists(), f"missing test fixture: {fixture}"
    return fixture


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.delenv("FIGURE_PROCESSOR", raising=False)
    config.settings.cache_clear()
    yield
    config.settings.cache_clear()


prismdoc = pytest.importorskip("prismdoc", reason="needs the ingest extra")


class TestFormatCoverage:
    """The platform no longer routes by format — prismdoc dispatches internally.

    What is still the platform's business is that discovery and parsing agree:
    `documents_under()` selects files by PARSEABLE, so a format listed here that
    prismdoc cannot load becomes a failed document, and one prismdoc handles but
    PARSEABLE omits is silently never ingested.
    """

    def test_declared_formats_match_what_prismdoc_loads(self):
        from prismdoc.stages.ingest import IngestStage

        assert parsing.PARSEABLE == set(IngestStage()._by_extension)

    def test_office_formats_are_covered(self):
        """The split that used to force a second parsing stack in this repo."""
        assert {".docx", ".pptx", ".html"} <= parsing.PARSEABLE


class TestFigureProcessorSelection:
    def test_off_is_the_default(self):
        assert config.settings().figure_processor == "off"
        assert parsing._figure_processor() is None

    def test_off_means_no_figure_stages_run(self, monkeypatch, real_pdf):
        """Not 'figures described as empty' — no figure work attempted at all."""
        monkeypatch.setenv("FIGURE_PROCESSOR", "off")
        config.settings.cache_clear()

        markdown = parsing.parse_to_markdown(real_pdf)

        assert "Catalog page with a figure" in markdown
        assert "[[FIGURE:" not in markdown, "a placeholder token leaked into the corpus"

    def test_vlm_selects_the_platform_model(self, monkeypatch):
        monkeypatch.setenv("FIGURE_PROCESSOR", "vlm")
        config.settings.cache_clear()

        from services.ingest.figures import BedrockFigureProcessor

        assert isinstance(parsing._figure_processor(), BedrockFigureProcessor)

    def test_an_unknown_value_fails_loudly(self, monkeypatch):
        """Silently falling back to 'off' would index no figures and say nothing."""
        monkeypatch.setenv("FIGURE_PROCESSOR", "vml")  # transposed
        config.settings.cache_clear()

        with pytest.raises(RuntimeError, match="FIGURE_PROCESSOR"):
            config.settings()


class TestFiguresReachTheMarkdown:
    """The whole point of the dependency, end to end on a real PDF."""

    def test_figure_text_is_merged_where_the_figure_sat(self, monkeypatch, real_pdf):
        from prismdoc.stages.figures import StubFigureProcessor

        monkeypatch.setattr(parsing, "_figure_processor", lambda: StubFigureProcessor())

        markdown = parsing.parse_to_markdown(real_pdf)

        assert "[figure fig_0_0" in markdown, "the figure never reached the Markdown"
        assert "[[FIGURE:" not in markdown, "an unmerged placeholder was left behind"
        # Position matters: the description belongs with the page it came from,
        # or retrieval returns it next to unrelated text.
        assert markdown.index("Catalog page with a figure") < markdown.index("[figure fig_0_0")

    def test_document_text_survives_figure_processing(self, monkeypatch, real_pdf):
        from prismdoc.stages.figures import StubFigureProcessor

        monkeypatch.setattr(parsing, "_figure_processor", lambda: StubFigureProcessor())

        markdown = parsing.parse_to_markdown(real_pdf)

        assert "Unit price 12.50 EUR" in markdown


class TestVectorDetection:
    """Inference, not extraction — so it has to be asked for."""

    def test_off_by_default(self):
        assert config.settings().detect_vector_figures is False

    def test_the_pdf_loader_inherits_the_setting(self, monkeypatch):
        monkeypatch.setenv("DETECT_VECTOR_FIGURES", "true")
        config.settings.cache_clear()

        pdf_loader = parsing._loaders()[0]

        assert pdf_loader._engine.detect_vector_figures is True

    def test_off_means_the_engine_is_told_off(self):
        assert parsing._loaders()[0]._engine.detect_vector_figures is False

    def test_every_format_still_has_a_loader(self):
        """The custom loader list must not silently drop a format."""
        from prismdoc.stages.ingest import IngestStage

        covered = set()
        for loader in parsing._loaders():
            covered.update(ext.lower() for ext in loader.extensions)

        assert covered == set(IngestStage()._by_extension)
