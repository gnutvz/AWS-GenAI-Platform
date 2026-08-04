"""Versioned prompts, loaded from disk instead of frozen into source.

A prompt is the most frequently changed and least reviewed part of an agent. As
a string literal it has no version, so a change to it is indistinguishable from
any other commit: the eval suite reports a score with nothing to attribute it
to, a regression cannot be rolled back without rolling back the deploy that
carried it, and a trace records what the model said but not what it was told.

Files, one per version, fix that cheaply. `v2.md` sitting next to `v1.md` is the
whole mechanism — the version is in the filename, the previous text is still on
disk, and `PROMPT_VERSION` pins a deployment to one of them.

What this deliberately is not: a prompt service with a database, an approval
flow and an admin UI. Those solve a problem that starts when non-engineers edit
prompts, which is not this repo's problem yet.

The loader lives in `aiplat` because any workload needs it. The prompts do not —
they belong to whichever service they instruct, which is why they sit under
`services/agent/prompts/` rather than here. Same test as everything else in this
package: would a second workload need it?
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

VERSION_FILE = re.compile(r"^v(\d+)\.md$")


@dataclass(frozen=True)
class Prompt:
    """One version of one prompt, and where it came from."""

    name: str
    version: int
    text: str

    @property
    def label(self) -> str:
        """What goes on a trace, an eval report and an answer: `system@v1`."""
        return f"{self.name}@v{self.version}"


def versions(directory: Path) -> list[int]:
    """Every version on disk, ascending. Empty if the directory has none."""
    if not directory.is_dir():
        return []
    found = [
        int(match.group(1))
        for path in directory.iterdir()
        if (match := VERSION_FILE.match(path.name))
    ]
    return sorted(found)


def load(directory: Path, version: int | None = None) -> Prompt:
    """Read one prompt version.

    Args:
        directory: Folder holding `v1.md`, `v2.md`, ...
        version: Which to load. None takes the highest on disk, which is right
            for development and for the eval suite. Deployments should pin
            `PROMPT_VERSION` so that adding a file is not the same act as
            shipping it.
    """
    available = versions(directory)
    if not available:
        raise RuntimeError(
            f"No prompt versions in {directory}. Expected at least one file named "
            f"v<N>.md — a prompt with no version is the thing this module exists to "
            f"prevent."
        )

    chosen = available[-1] if version is None else version
    if chosen not in available:
        raise RuntimeError(
            f"Prompt version v{chosen} not found in {directory}. "
            f"Available: {', '.join(f'v{v}' for v in available)}."
        )

    return Prompt(
        name=directory.name,
        version=chosen,
        text=(directory / f"v{chosen}.md").read_text(encoding="utf-8").strip(),
    )
