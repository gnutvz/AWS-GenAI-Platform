"""Document → Markdown.

Parsing quality decides retrieval quality, so it is worth owning — but owning it
does not mean doing it here. prismdoc owns parsing: which engine reads which
format, how tables are rendered, how figures are cut out and merged back. This
module hands it a file and takes Markdown.

That boundary is the point. An earlier version of this file knew that prismdoc
could not load DOCX and routed those to docling itself, which meant every format
prismdoc gained or lost was a change here too. Consumers should not have to track
another project's internals to use it.

What the platform still owns is the *decision* prismdoc cannot make for it: what
a figure should become. `FIGURE_PROCESSOR` picks, and `BedrockFigureProcessor`
describes figures with the model `aiplat.llm` already hands out — so the figure
path inherits the configured route, region and credentials rather than growing a
second way to reach a model.

Figures are off by default. Describing every figure in a corpus is one model call
per figure, and a 5,189-document corpus should meet that as a decision rather
than an invoice.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiplat.config import settings

logger = logging.getLogger(__name__)

# Formats prismdoc's IngestStage accepts. Kept as a literal set rather than
# imported from prismdoc so that discovery does not need the ingest extra
# installed — `documents_under()` runs wherever the CLI does.
PARSEABLE = {
    ".pdf",
    ".docx",
    ".pptx",
    ".html",
    ".htm",
    ".xlsx",
    ".md",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".webp",
    ".gif",
}

_PRISMDOC_HINT = "prismdoc is not installed. Run: pip install -e '.[ingest]'"


def parse_to_markdown(path: Path) -> str:
    """Convert one document to Markdown, preserving tables and figures."""
    try:
        from prismdoc import (
            Context,
            Document,
            FigureExtractStage,
            FigureMergeStage,
            FigureProcessStage,
            IngestStage,
            ParseStage,
            Pipeline,
            Source,
        )
    except ImportError as exc:
        raise SystemExit(_PRISMDOC_HINT) from exc

    stages = [IngestStage(), ParseStage(parser=_parser())]

    processor = _figure_processor()
    if processor is not None:
        # Three stages rather than one: extract cuts figures out and leaves a
        # placeholder, process turns each into text, merge substitutes it back
        # where the figure sat. The split is what lets the description come from
        # a model prismdoc knows nothing about.
        stages += [
            FigureExtractStage(),
            FigureProcessStage(processor=processor),
            FigureMergeStage(),
        ]

    document = Document(source=Source(path=str(path)))
    result = Pipeline(stages).run(document, Context())
    return result.artifacts.get("parsed_markdown", "")


def _parser():
    """Passthrough: let each loader's own reader produce the text.

    Not `parser.docling`, which would re-convert the file and discard what the
    loader already did — and, more importantly, returns whole-document Markdown
    with no page markers, which strands every figure at the end of the document
    instead of on its page. Passthrough keeps prismdoc's per-page structure,
    which figure placement depends on.
    """
    from prismdoc import registry

    return registry.create("parser.passthrough")


def _figure_processor():
    """Whatever `FIGURE_PROCESSOR` names, or None to skip figures entirely."""
    choice = settings().figure_processor

    if choice == "off":
        return None

    if choice == "ocr":
        # Reads text *inside* a figure — labels on a schematic, values in a chart
        # image. Cheap and local, but says nothing about a diagram that carries
        # its meaning in structure rather than words.
        from prismdoc.stages.figures import OcrFigureProcessor

        return OcrFigureProcessor()

    from services.ingest.figures import BedrockFigureProcessor

    return BedrockFigureProcessor()
