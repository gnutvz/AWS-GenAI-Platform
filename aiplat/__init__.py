"""Shared platform layer: configuration, model construction, tracing, retrieval.

Services import from here. Nothing in here imports from services.
"""

from aiplat.config import Settings, settings
from aiplat.llm import build_model
from aiplat.telemetry import setup_tracing, trace_attributes

__all__ = ["Settings", "build_model", "settings", "setup_tracing", "trace_attributes"]
