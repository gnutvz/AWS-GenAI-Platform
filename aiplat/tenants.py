"""Tenant definitions — one YAML file per customer.

A tenant owns *data and labels*: where its documents come from, what language
they are in, which eval set scores it. It does not own *behaviour* — the system
prompt, chunking, guardrail and agent logic are the platform's, identical for
everyone. That line is what keeps this a platform with several tenants rather
than several forks wearing a config file.

Each tenant gets its own knowledge base. Isolation is then an IAM boundary
rather than a filter someone can forget to apply: a tenant's agent role is
scoped to exactly one knowledge base ARN, so cross-tenant retrieval is not a bug
you can write, it is a permission you do not hold.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

TENANTS_DIR = Path("tenants")

# Used in stack names, bucket names and IAM resources — so it has to be
# lowercase-safe and short enough to leave room for the suffixes CDK appends.
SLUG = re.compile(r"^[a-z][a-z0-9-]{1,20}$")

# Embedding model per language. Titan v2 is multilingual and the safe default;
# a tenant whose corpus is entirely one language may do better elsewhere. This
# is the one behavioural knob a tenant gets, because a shared knowledge base
# could not offer it at all.
EMBEDDING_BY_LANGUAGE = {
    "en": ("amazon.titan-embed-text-v2:0", 1024),
    "ja": ("amazon.titan-embed-text-v2:0", 1024),
    "vi": ("amazon.titan-embed-text-v2:0", 1024),
}
DEFAULT_EMBEDDING = EMBEDDING_BY_LANGUAGE["en"]


@dataclass(frozen=True)
class Source:
    """One directory of documents to ingest."""

    path: str
    doc_type: str = "general"
    # Documents missing any of these metadata keys are refused at ingest.
    # For technical datasheets this is the guard that stops the agent answering
    # confidently from a superseded revision: no effective date, no index entry.
    require_metadata: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Tenant:
    slug: str
    display_name: str
    language: str = "en"
    sources: list[Source] = field(default_factory=list)
    eval_dataset: str | None = None
    generate_eval_from_corpus: bool = False

    @property
    def embedding(self) -> tuple[str, int]:
        """(model_id, dimension). Changing this means re-indexing the tenant."""
        return EMBEDDING_BY_LANGUAGE.get(self.language, DEFAULT_EMBEDDING)

    @property
    def stack_suffix(self) -> str:
        return self.slug

    def __post_init__(self) -> None:
        if not SLUG.match(self.slug):
            raise ValueError(
                f"Tenant slug {self.slug!r} must be lowercase letters, digits and "
                f"hyphens, 2-21 characters, starting with a letter — it becomes part "
                f"of stack and bucket names."
            )


def _parse(data: dict, origin: Path) -> Tenant:
    missing = {"tenant", "display_name"} - data.keys()
    if missing:
        raise ValueError(f"{origin}: missing required field(s): {', '.join(sorted(missing))}")

    unknown = data.keys() - {
        "tenant",
        "display_name",
        "language",
        "sources",
        "eval",
    }
    if unknown:
        # A typo in a config key is otherwise silent — the setting simply never
        # takes effect, which is worse than failing here.
        raise ValueError(f"{origin}: unknown field(s): {', '.join(sorted(unknown))}")

    sources = []
    for entry in data.get("sources") or []:
        if "path" not in entry:
            raise ValueError(f"{origin}: every source needs a 'path'")
        sources.append(
            Source(
                path=entry["path"],
                doc_type=entry.get("doc_type", "general"),
                require_metadata=list(entry.get("require_metadata") or []),
            )
        )

    eval_config = data.get("eval") or {}
    return Tenant(
        slug=data["tenant"],
        display_name=data["display_name"],
        language=data.get("language", "en"),
        sources=sources,
        eval_dataset=eval_config.get("dataset"),
        generate_eval_from_corpus=bool(eval_config.get("generate_from_corpus", False)),
    )


def load(path: Path) -> Tenant:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        # ValueError rather than TypeError: from the caller's point of view this
        # is a malformed config file, not a programming mistake.
        raise ValueError(f"{path}: expected a YAML mapping")  # noqa: TRY004
    return _parse(data, path)


def load_all(directory: Path = TENANTS_DIR) -> list[Tenant]:
    """Every tenant defined on disk, sorted for deterministic stack ordering.

    Files starting with `_` are ignored, so `_example.yaml` can document the
    format without provisioning anything.
    """
    if not directory.exists():
        return []

    tenants = [
        load(path)
        for path in sorted(directory.glob("*.yaml"))
        if not path.name.startswith("_")
    ]

    slugs = [t.slug for t in tenants]
    duplicates = {s for s in slugs if slugs.count(s) > 1}
    if duplicates:
        # Two files claiming one slug would silently collide on stack names.
        raise ValueError(f"Duplicate tenant slug(s): {', '.join(sorted(duplicates))}")

    return tenants


def get(slug: str, directory: Path = TENANTS_DIR) -> Tenant:
    for tenant in load_all(directory):
        if tenant.slug == slug:
            return tenant
    known = ", ".join(t.slug for t in load_all(directory)) or "none defined"
    raise SystemExit(f"Unknown tenant {slug!r}. Known tenants: {known}")
