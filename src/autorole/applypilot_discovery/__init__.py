"""Standalone discovery package for ApplyPilot.

The discovery implementation lives here so it can be extracted into its own
distribution later. Host application concerns such as config loading,
database access, and LLM client creation are routed through the runtime
adapter in applypilot_discovery.runtime.
"""

from .jobspy import run_discovery
from .smartextract import run_smart_extract
from .workday import run_workday_discovery

__all__ = [
    "run_discovery",
    "run_smart_extract",
    "run_workday_discovery",
]