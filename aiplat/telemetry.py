"""Tracing setup.

Strands emits OpenTelemetry spans for every model call and tool invocation. Point
them at any OTLP collector — Langfuse for prompt-level debugging and cost, ADOT if
the org already standardised on CloudWatch/X-Ray.

Deliberately OTLP rather than a vendor SDK: traces and eval datasets are the assets
worth keeping portable, and they are exactly what gets locked in if you let a
proprietary client own them.
"""

from __future__ import annotations

import logging

from aiplat.config import settings

logger = logging.getLogger(__name__)

_initialised = False


def setup_tracing() -> bool:
    """Idempotent. Returns True if an exporter was wired up.

    Safe to call on every Lambda invocation — the module-level guard means only the
    first call in a warm container does work.
    """
    global _initialised
    if _initialised:
        return True

    cfg = settings()
    if not cfg.tracing_enabled:
        logger.debug("OTEL_EXPORTER_OTLP_ENDPOINT unset; running untraced")
        return False

    try:
        from strands.telemetry import StrandsTelemetry
    except ImportError:
        logger.warning("Tracing requested but otel extra not installed: pip install 'aiplat[otel]'")
        return False

    # StrandsTelemetry reads OTEL_EXPORTER_OTLP_ENDPOINT and
    # OTEL_EXPORTER_OTLP_HEADERS from the environment directly.
    StrandsTelemetry().setup_otlp_exporter()
    _initialised = True
    logger.info("Tracing to %s as %s", cfg.otlp_endpoint, cfg.service_name)
    return True


def trace_attributes(**extra: str) -> dict[str, str]:
    """Attributes stamped on every span so traces can be sliced per tenant/env."""
    cfg = settings()
    attrs = {"service.name": cfg.service_name, "llm.route": cfg.llm_route}
    attrs.update({k: v for k, v in extra.items() if v})
    return attrs
