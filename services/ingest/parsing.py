"""Document → Markdown, via prismdoc.

Parsing quality decides retrieval quality, so this is the one part of the RAG
pipeline worth owning rather than delegating to the knowledge base. What changed
is *what* we own it with.

Calling docling directly extracted text, headings and tables well and dropped
everything else on the floor. Docling's enrichment features — picture
description, picture classification, formula and code understanding — are all
disabled by default, and nothing here turned them on. So a diagram, a chart, a
screenshot or a photographed form became an empty placeholder in the Markdown:
no text, no description, no warning. The document logged as ingested and the
agent could never answer a question about the picture in it.

prismdoc closes that, and the figure sub-pipeline is the reason to depend on it:
figures are cut out of the page, described by a model, and merged back into the
Markdown where they sat. A diagram becomes prose the retriever can index.

Two boundaries worth stating plainly:

**prismdoc does not load every format.** It reads PDF, XLSX, images and plain
text. DOCX, PPTX and HTML have no loader there, so those still go to docling
directly. That split is not elegant, and it is honest: the alternative is
pretending one stack covers everything.

**Figure processing is off by default.** Describing every figure in a corpus
means one model call per figure — on the 5,189-document benchmark corpus that is
a real bill arriving without anyone choosing it. `FIGURE_PROCESSOR` is the
choice, and `off` is what you get if you do not make it.
"""

from __future__ import annotations

import logging
from pathlib import Path

from aiplat.config import settings

logger = logging.getLogger(__name__)

# What prismdoc can load itself. Everything else falls through to docling.
PRISMDOC_FORMATS = {".pdf", ".xlsx", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
PASSTHROUGH = {".md", ".txt"}
DOCLING_ONLY = {".docx", ".pptx", ".html"}

PARSEABLE = PRISMDOC_FORMATS | PASSTHROUGH | DOCLING_ONLY

_PRISMDOC_HINT = "prismdoc is not installed. Run: pip install -e '.[ingest]'"
_DOCLING_HINT = "docling is not installed. Run: pip install -e '.[ingest]'"


def parse_to_markdown(path: Path) -> str:
    """Convert one document to Markdown, preserving tables and figures."""
    suffix = path.suffix.lower()

    if suffix in PASSTHROUGH:
        return path.read_text(encoding="utf-8", errors="replace")

    if suffix in DOCLING_ONLY:
        return _docling_markdown(path)

    return _prismdoc_markdown(path)


def _prismdoc_markdown(path: Path) -> str:
    """Run the prismdoc pipeline: parse, then extract/describe/merge figures."""
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

    stages = [IngestStage(), ParseStage(parser=_best_parser())]

    processor = _figure_processor()
    if processor is not None:
        # Extract cuts figures out and leaves a placeholder token; process turns
        # each into text; merge substitutes the text back where the figure sat.
        # Splitting it three ways is what lets the description come from a model
        # this package knows nothing about.
        stages += [
            FigureExtractStage(),
            FigureProcessStage(processor=processor),
            FigureMergeStage(),
        ]

    document = Document(source=Source(path=str(path)))
    result = Pipeline(stages).run(document, Context())
    return result.artifacts.get("parsed_markdown", "")


def _best_parser():
    """Docling when it is installed, pdfplumber otherwise.

    Docling runs a layout model and reads borderless and scanned tables that
    pdfplumber's line-based detection misses. pdfplumber is the permissive
    fallback that keeps a lighter install working rather than failing — for
    born-digital PDFs with ruled tables the gap is small.
    """
    from prismdoc import registry

    try:
        import docling  # noqa: F401
    except ImportError:
        logger.info("docling not installed; parsing PDFs with pdfplumber")
        return registry.create("parser.pdfplumber")
    return registry.create("parser.docling")


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


def _docling_markdown(path: Path) -> str:
    """Formats prismdoc has no loader for."""
    try:
        from docling.document_converter import DocumentConverter
    except ImportError as exc:
        raise SystemExit(_DOCLING_HINT) from exc

    return DocumentConverter().convert(str(path)).document.export_to_markdown()
