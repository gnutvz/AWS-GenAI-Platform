"""Single source of truth for configuration.

Every value comes from the environment. In Lambda that environment is populated
by CDK; locally it comes from .env. Nothing reads os.environ outside this module,
so swapping in SSM/Secrets Manager later is one change here rather than a grep
across the codebase.

Loading .env happens here, on import, for the same reason. It used to live in
`scripts/ask.py` alone, which meant `make ask` read the file and `make ingest`,
`make eval` and `make chat` did not — so a machine configured entirely through
.env could ask the deployed agent and fail at everything else, with an error
about a missing setting rather than a missing file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

LlmRoute = Literal["bedrock", "gateway"]
FigureProcessor = Literal["off", "ocr", "vlm"]

DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-6"


def load_dotenv(path: Path | None = None) -> None:
    """Read a .env file into the process environment, if one is there.

    Deliberately not python-dotenv: this is a dev convenience, and the Lambda
    bundle should not carry a dependency for a file that never exists there.

    `setdefault`, so a real environment variable always beats the file. That is
    what lets `AWS_PROFILE=other make eval` do what it looks like it does, and
    what stops a stale .env quietly overriding a deliberate export.
    """
    path = path or Path(".env")
    if not path.exists():
        return

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        # Strip quotes: a value pasted from a console often arrives wrapped, and
        # an ARN with a leading quote fails much later and much less clearly.
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


# On import rather than inside settings(): boto3 clients elsewhere read
# credentials straight from os.environ, and they must find what .env supplied.
load_dotenv()


def _get(name: str, default: str = "") -> str:
    """A setting, where blank means absent.

    `os.environ.get(name, default)` returns "" for a variable that exists and is
    empty, so `MODEL_ID=` in a .env file silently beat the default and the agent
    was constructed with no model id at all. That is not a hypothetical: the
    handover template hands someone a file and tells them to leave optional
    fields blank, which is precisely how a key comes to exist with no value.
    """
    return os.environ.get(name, "").strip() or default


def _optional(name: str) -> str | None:
    """Empty string and unset mean the same thing: feature is off."""
    return _get(name) or None


def _flag(name: str) -> bool:
    """Anything other than an explicit yes is a no."""
    return _get(name).lower() in ("1", "true", "yes", "on")


def _choice(name: str, allowed: tuple[str, ...], default: str) -> str:
    """One of a fixed set, or a loud failure.

    A typo here would otherwise silently select the default — which for
    FIGURE_PROCESSOR means an ingest run that quietly indexes no figures at all.
    """
    value = _get(name).lower() or default
    if value not in allowed:
        raise RuntimeError(f"{name} must be one of {', '.join(allowed)}, got {value!r}")
    return value


def _version(name: str) -> int | None:
    """A pinned version number, accepting both `2` and `v2`."""
    raw = _get(name).lstrip("vV")
    if not raw:
        return None
    if not raw.isdigit():
        # Silently ignoring this would run an unpinned prompt in a deployment
        # whose whole point was to pin one.
        raise RuntimeError(f"{name} must be a version number like 2 or v2, got {_get(name)!r}")
    return int(raw)


@dataclass(frozen=True)
class Settings:
    region: str
    # Which tenant this deployment serves. Comes from the environment, never from
    # the request — a caller must not be able to label itself as someone else, or
    # traces and cost attribution become fiction.
    tenant: str
    llm_route: LlmRoute
    model_id: str

    gateway_base_url: str | None
    gateway_api_key: str | None
    # Acknowledges that routing through the gateway drops native guardrail
    # enforcement. Without it, configuring both is refused rather than silently
    # honouring only one — see aiplat/llm.py.
    gateway_allow_unguarded: bool

    guardrail_id: str | None
    guardrail_version: str

    knowledge_base_id: str | None
    documents_bucket: str | None

    session_bucket: str | None

    otlp_endpoint: str | None
    service_name: str

    # Which prompt this deployment serves — a directory under the workload's
    # prompts/. Set per tenant, so two tenants sharing an account can be answered
    # by differently-instructed agents.
    prompt_name: str

    # Which prompt version this deployment serves. None means "highest on disk",
    # which is convenient locally and wrong in production: adding a file should
    # not be the same act as shipping it.
    prompt_version: int | None

    # How figures in ingested documents become searchable text: off | ocr | vlm.
    # Off by default because `vlm` is one model call per figure, and a corpus
    # discovers that as an invoice rather than a decision.
    figure_processor: FigureProcessor

    # Also treat clusters of lines and fills as figures — schematics drawn rather
    # than embedded. Off by default because it is inference, not extraction: a
    # wrong guess spends a model call and files a description of a page border in
    # the index as though it were content.
    detect_vector_figures: bool

    @property
    def tracing_enabled(self) -> bool:
        return self.otlp_endpoint is not None

    @property
    def guardrail_enabled(self) -> bool:
        return self.guardrail_id is not None

    @property
    def retrieval_enabled(self) -> bool:
        return self.knowledge_base_id is not None

    def require(self, attr: str) -> str:
        """Fail loudly at the point of use rather than silently degrading."""
        value = getattr(self, attr, None)
        if not value:
            raise RuntimeError(
                f"Setting '{attr}' is required for this operation but is not configured. "
                f"See .env.example."
            )
        return value


@lru_cache(maxsize=1)
def settings() -> Settings:
    route = _get("LLM_ROUTE", "bedrock").lower()
    if route not in ("bedrock", "gateway"):
        raise RuntimeError(f"LLM_ROUTE must be 'bedrock' or 'gateway', got {route!r}")

    return Settings(
        region=_get("AWS_REGION") or _get("AWS_DEFAULT_REGION", "us-west-2"),
        tenant=_get("TENANT", "default"),
        llm_route=route,  # type: ignore[arg-type]
        model_id=_get("MODEL_ID", DEFAULT_MODEL_ID),
        gateway_base_url=_optional("GATEWAY_BASE_URL"),
        gateway_api_key=_optional("GATEWAY_API_KEY"),
        gateway_allow_unguarded=_flag("GATEWAY_ALLOW_UNGUARDED"),
        guardrail_id=_optional("GUARDRAIL_ID"),
        guardrail_version=_get("GUARDRAIL_VERSION", "DRAFT"),
        knowledge_base_id=_optional("KNOWLEDGE_BASE_ID"),
        documents_bucket=_optional("DOCUMENTS_BUCKET"),
        session_bucket=_optional("SESSION_BUCKET"),
        otlp_endpoint=_optional("OTEL_EXPORTER_OTLP_ENDPOINT"),
        service_name=_get("OTEL_SERVICE_NAME", "aiplat-agent"),
        prompt_name=_get("PROMPT_NAME", "system"),
        prompt_version=_version("PROMPT_VERSION"),
        figure_processor=_choice("FIGURE_PROCESSOR", ("off", "ocr", "vlm"), "off"),
        detect_vector_figures=_flag("DETECT_VECTOR_FIGURES"),
    )
