"""Tenant definitions — one YAML file per customer.

A tenant owns *data and labels*: where its documents come from, what language
they are in, which eval set scores it. It also owns a narrow slice of
*behaviour* — which prompt answers its questions and which model does the
answering.

That second part was deliberately absent while there was one use case, on the
argument that shared behaviour is what separates a platform from several forks
wearing a config file. It stopped holding the moment more than one department
plugged in: a legal team and an engineering team asking questions of their own
corpora do not want the same instructions, and telling them the platform's
prompt is the platform's is telling them to fork it.

What a tenant still does *not* own: chunking, the guardrail, retrieval, and the
agent's logic. Those are the platform's, identical for everyone, because they
are the parts an auditor asks about and the parts a mistake in is expensive.

Nothing about the mechanism is new. `infra/app.py` already deploys one Lambda
per tenant with its own environment, so per-tenant behaviour is a value flowing
through a path that exists — not a config service resolved at request time.

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
class AgentConfig:
    """How this tenant's agent differs from the default one.

    Two knobs, both with an immediate use. There is no `tools` field: exactly one
    tool exists, so a list to choose from would be configuration for a decision
    nobody can make yet — the thing this repo's own guidance calls waiting for
    the second caller. When a second tool lands, this is where it goes.
    """

    # Directory under services/agent/prompts/. The default is the prompt every
    # tenant shared before this existed, so an unchanged YAML behaves unchanged.
    prompt: str = "system"
    # None means "highest version on disk" — right for a tenant iterating on its
    # own prompt, wrong for one that wants a fixed answer, which is why it can
    # be pinned per tenant rather than only per deployment.
    prompt_version: int | None = None
    # None means the deployment's MODEL_ID. A tenant with cheap, high-volume
    # questions and one doing hard analysis should not be forced onto one model
    # because they share an account.
    model: str | None = None


@dataclass(frozen=True)
class Tenant:
    slug: str
    display_name: str
    language: str = "en"
    sources: list[Source] = field(default_factory=list)
    eval_dataset: str | None = None
    generate_eval_from_corpus: bool = False
    agent: AgentConfig = field(default_factory=AgentConfig)

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
        "agent",
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
        agent=_parse_agent(data.get("agent") or {}, origin),
    )


def _parse_agent(data: dict, origin: Path) -> AgentConfig:
    unknown = data.keys() - {"prompt", "prompt_version", "model"}
    if unknown:
        raise ValueError(f"{origin}: unknown agent field(s): {', '.join(sorted(unknown))}")

    version = data.get("prompt_version")
    if version is not None:
        # Accept `2` and `v2`, matching PROMPT_VERSION. A tenant file is edited by
        # hand, so the spelling that reads naturally should not be an error.
        try:
            version = int(str(version).lower().lstrip("v"))
        except ValueError:
            raise ValueError(
                f"{origin}: agent.prompt_version must be a number like 2 or v2, "
                f"got {data['prompt_version']!r}"
            ) from None

    return AgentConfig(
        prompt=data.get("prompt", "system"),
        prompt_version=version,
        model=data.get("model"),
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
