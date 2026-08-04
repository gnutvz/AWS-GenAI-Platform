"""The one place that constructs a model.

This is the seam that matters most. Application code asks for `build_model()` and
never imports a provider directly, so moving from Bedrock to a LiteLLM gateway
(per-team budgets, virtual keys, non-Bedrock models) is a config flip, not a
rewrite.

Trade-off worth knowing: Bedrock Guardrails attach natively to BedrockModel. Route
through the gateway and you lose that binding, so guardrail enforcement has to move
to the proxy or to an explicit ApplyGuardrail call. That is the real cost of the
gateway, and it is why `bedrock` is the default.

That cost used to be documented here and nowhere else — `_gateway_model` simply
never looked at the guardrail settings, so deploying with both configured ran
unguarded and said nothing. A safety control that disappears quietly when an
unrelated flag changes is worse than one that was never configured, because the
policy artifact still exists and still passes review. Configuring both is now
refused unless the operator says out loud that enforcement lives somewhere else.
"""

from __future__ import annotations

import logging
from typing import Any

from strands.models import BedrockModel
from strands.models.model import Model

from aiplat.aws import boto_config
from aiplat.config import Settings, settings

logger = logging.getLogger(__name__)


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
        # Bedrock throttles on tokens per minute, so this is the call most likely
        # to be rate-limited in normal operation, not in an incident.
        "boto_client_config": boto_config(),
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
    _check_guardrail_coverage(cfg)

    # Imported lazily so the Lambda bundle does not carry litellm unless used.
    from strands.models.litellm import LiteLLMModel

    client_args: dict[str, Any] = {"base_url": cfg.require("gateway_base_url")}
    if cfg.gateway_api_key:
        client_args["api_key"] = cfg.gateway_api_key

    params: dict[str, Any] = {"model_id": cfg.model_id}
    params.update(extra)
    return LiteLLMModel(client_args=client_args, **params)


def _check_guardrail_coverage(cfg: Settings) -> None:
    """Refuse the combination that looks protected and is not.

    A guardrail configured while routing through the gateway is not applied by
    this seam. Failing here is deliberate: the alternative is a deployment that
    passes a compliance review on the strength of a guardrail ARN it never calls.
    """
    if not cfg.guardrail_enabled:
        return

    if not cfg.gateway_allow_unguarded:
        raise RuntimeError(
            f"GUARDRAIL_ID is set ({cfg.guardrail_id}) but LLM_ROUTE=gateway does not "
            f"enforce it — Bedrock Guardrails bind to the Bedrock model, not to the "
            f"proxy. Pick one:\n"
            f"  • LLM_ROUTE=bedrock            — keep native enforcement (the default)\n"
            f"  • enforce it in the gateway    — then set GATEWAY_ALLOW_UNGUARDED=true\n"
            f"  • unset GUARDRAIL_ID           — if you genuinely want no guardrail\n"
            f"Refusing to start rather than run unguarded while a guardrail is configured."
        )

    # Opted in, so this is a choice rather than an accident — but it still belongs
    # in the log of any deployment an auditor might read.
    logger.warning(
        "LLM_ROUTE=gateway with GUARDRAIL_ID=%s: native guardrail enforcement is NOT "
        "applied. GATEWAY_ALLOW_UNGUARDED=true, so enforcement is assumed to live in "
        "the gateway.",
        cfg.guardrail_id,
    )
