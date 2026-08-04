"""Shared platform layer: configuration, model construction, tracing, retrieval.

Services import from here. Nothing in here imports from services.
"""

from importlib.metadata import PackageNotFoundError, version

from aiplat import prompts
from aiplat.config import Settings, settings
from aiplat.llm import build_model
from aiplat.telemetry import setup_tracing, trace_attributes

try:
    __version__ = version("aiplat")
except PackageNotFoundError:  # pragma: no cover - running from a source tree
    __version__ = "0.0.0+unknown"

__all__ = [
    "Settings",
    "__version__",
    "build_model",
    "prompts",
    "settings",
    "setup_tracing",
    "trace_attributes",
]
