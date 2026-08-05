"""Turn a figure into text the retriever can index, using the platform's model.

prismdoc extracts figures and merges text back where they sat; it does not
decide what that text says. This is the platform's answer: describe the figure
with the same model `aiplat.llm` hands out everywhere else, so the figure path
inherits the routing, the region and the credentials already configured rather
than growing a second way to reach a model.

What the description is *for* shapes the prompt. This text is not shown to
anyone — it is embedded and retrieved. So it should read like the sentence
someone would type to look for this figure: the entities, labels and
relationships in it, stated plainly. Aesthetic description ("a clean modern
diagram") retrieves nothing and costs tokens.
"""

from __future__ import annotations

import base64
import logging

from prismdoc.stages.figures import Figure, FigureProcessor

from aiplat import build_model

logger = logging.getLogger(__name__)

# Bedrock accepts these; anything else has to be converted before it is sent.
SUPPORTED_FORMATS = {"png", "jpeg", "gif", "webp"}

DESCRIBE_PROMPT = """You are describing a figure extracted from an enterprise document so that it can be found by search.

Write what the figure shows, in plain prose:
- Name every label, axis, entity, component and value you can read.
- State the relationships: what connects to what, what flows where, what the trend is.
- If it is a table rendered as an image, transcribe it.
- If it is a photograph or scan, say what it depicts and transcribe any text.

Do not describe style, colour or layout unless they carry meaning. Do not begin
with "This image shows". If the figure is decorative and carries no information,
reply with exactly: NO INFORMATION
"""

# The model's way of saying the figure is a logo or a divider. Merging that into
# the document would put noise where the retriever expects content.
EMPTY_MARKER = "NO INFORMATION"


class BedrockFigureProcessor(FigureProcessor):
    """Describe a figure with the platform's configured model."""

    def __init__(self, prompt: str = DESCRIBE_PROMPT) -> None:
        self._prompt = prompt

    def process(self, figure: Figure) -> str:
        image_format = _bedrock_format(figure.mime)
        if image_format is None:
            logger.warning("Skipping figure %s: unsupported mime %s", figure.id, figure.mime)
            return ""

        try:
            description = self._describe(figure, image_format)
        except Exception as exc:  # noqa: BLE001 — one figure must not fail the document
            logger.warning("Could not describe figure %s: %s", figure.id, exc)
            return ""

        # Stripped here rather than trusting _describe to have done it: a
        # whitespace-only answer is truthy, and would otherwise be merged into
        # the corpus as a figure label with nothing after it.
        description = (description or "").strip()
        if not description or EMPTY_MARKER in description.upper():
            return ""

        # Marked so a retrieved passage says where its text came from. A reader
        # seeing a confident paragraph should be able to tell it was read off a
        # picture by a model, not lifted from the document's prose.
        return f"**Figure {figure.id}:** {description}"

    def _describe(self, figure: Figure, image_format: str) -> str:
        from strands import Agent

        agent = Agent(model=build_model(), system_prompt=self._prompt)
        result = agent(
            [
                {
                    "image": {
                        "format": image_format,
                        "source": {"bytes": base64.b64decode(figure.image_b64)},
                    }
                },
                {"text": "Describe this figure."},
            ]
        )
        return str(result).strip()


def _bedrock_format(mime: str) -> str | None:
    """Map a mime type onto the format names Bedrock accepts."""
    subtype = mime.rsplit("/", 1)[-1].lower()
    if subtype == "jpg":
        subtype = "jpeg"
    return subtype if subtype in SUPPORTED_FORMATS else None
