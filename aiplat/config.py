"""Single source of truth for configuration.

Every value comes from the environment. In Lambda that environment is populated
by CDK; locally it comes from .env. Nothing reads os.environ outside this module,
so swapping in SSM/Secrets Manager later is one change here rather than a grep
across the codebase.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

LlmRoute = Literal["bedrock", "gateway"]

DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-4-6"


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _optional(name: str) -> str | None:
    """Empty string and unset mean the same thing: feature is off."""
    return _get(name) or None


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

    guardrail_id: str | None
    guardrail_version: str

    knowledge_base_id: str | None
    documents_bucket: str | None

    session_bucket: str | None

    otlp_endpoint: str | None
    service_name: str

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
        guardrail_id=_optional("GUARDRAIL_ID"),
        guardrail_version=_get("GUARDRAIL_VERSION", "DRAFT"),
        knowledge_base_id=_optional("KNOWLEDGE_BASE_ID"),
        documents_bucket=_optional("DOCUMENTS_BUCKET"),
        session_bucket=_optional("SESSION_BUCKET"),
        otlp_endpoint=_optional("OTEL_EXPORTER_OTLP_ENDPOINT"),
        service_name=_get("OTEL_SERVICE_NAME", "aiplat-agent"),
    )
