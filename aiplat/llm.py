"""The one place that constructs a model.

This is the seam that matters most. Application code asks for `build_model()` and
never imports a provider directly, so moving from Bedrock to a LiteLLM gateway
(per-team budgets, virtual keys, non-Bedrock models) is a config flip, not a
rewrite.

Trade-off worth knowing: Bedrock Guardrails attach natively to BedrockModel. Route
through the gateway and you lose that binding, so guardrail enforcement has to move
to the proxy or to an explicit ApplyGuardrail call. That is the real cost of the
gateway, and it is why `bedrock` is the default.
"""

from __future__ import annotations

from typing import Any

from strands.models import BedrockModel
from strands.models.model import Model

from aiplat.config import Settings, settings


def build_model(overrides: dict[str, Any] | None = None) -> Model:
    cfg = settings()
    extra = overrides or {}

    if cfg.llm_route == "gateway":
        return _gateway_model(cfg, extra)
    return _bedrock_model(cfg, extra)


def _bedrock_model(cfg: Settings, extra: dict[str, Any]) -> Model:
    params: dict[str, Any] = {
        "model_id": cfg.model_id,
        "region_name": cfg.region,
        # Prompt caching pays for itself as soon as the system prompt carries
        # retrieved context or a large tool catalogue.
        "cache_prompt": "default",
    }

    if cfg.guardrail_enabled:
        params.update(
            guardrail_id=cfg.guardrail_id,
            guardrail_version=cfg.guardrail_version,
            # Redact rather than hard-fail: a blocked answer the user can see the
            # shape of beats an opaque 500.
            guardrail_redact_input=True,
            guardrail_redact_output=True,
            guardrail_trace="enabled",
        )

    params.update(extra)
    return BedrockModel(**params)


def _gateway_model(cfg: Settings, extra: dict[str, Any]) -> Model:
    # Imported lazily so the Lambda bundle does not carry litellm unless used.
    from strands.models.litellm import LiteLLMModel

    client_args: dict[str, Any] = {"base_url": cfg.require("gateway_base_url")}
    if cfg.gateway_api_key:
        client_args["api_key"] = cfg.gateway_api_key

    params: dict[str, Any] = {"model_id": cfg.model_id}
    params.update(extra)
    return LiteLLMModel(client_args=client_args, **params)
