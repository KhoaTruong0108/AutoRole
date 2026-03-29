"""Runtime adapter for the discovery package.

This module centralizes the ApplyPilot host dependencies needed by the
discovery code. When discovery is moved into a separate project, the host can
inject its own runtime through configure_runtime without changing scraper
logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(slots=True)
class DiscoveryRuntime:
    """Host services required by the discovery package."""

    load_search_config: Callable[[], dict]
    get_connection: Callable[..., Any]
    init_db: Callable[..., Any]
    get_stats: Callable[..., dict]
    get_llm_client: Callable[[], Any]
    config_dir: Path


_runtime: DiscoveryRuntime | None = None


def _build_applypilot_runtime() -> DiscoveryRuntime:
    """Default runtime — only works inside the applypilot package itself.

    Any other consumer must call configure_runtime() before using discovery
    functions. See AGENT_INTEGRATION.md for a complete wiring example.
    """
    try:
        from applypilot import config
        from applypilot.database import get_connection, get_stats, init_db
        from applypilot.llm import get_client

        return DiscoveryRuntime(
            load_search_config=config.load_search_config,
            get_connection=get_connection,
            init_db=init_db,
            get_stats=get_stats,
            get_llm_client=get_client,
            config_dir=config.CONFIG_DIR,
        )
    except ImportError:
        raise RuntimeError(
            "applypilot_discovery: no runtime configured.\n"
            "Call configure_runtime(DiscoveryRuntime(...)) before using any "
            "discovery function. See AGENT_INTEGRATION.md for a wiring example."
        )


def configure_runtime(runtime: DiscoveryRuntime) -> None:
    """Override the default ApplyPilot-backed runtime."""
    global _runtime
    _runtime = runtime


def get_runtime() -> DiscoveryRuntime:
    """Return the active runtime, creating the default lazily."""
    global _runtime
    if _runtime is None:
        _runtime = _build_applypilot_runtime()
    return _runtime


def load_search_config() -> dict:
    return get_runtime().load_search_config()


def get_connection(*args, **kwargs):
    return get_runtime().get_connection(*args, **kwargs)


def init_db(*args, **kwargs):
    return get_runtime().init_db(*args, **kwargs)


def get_stats(*args, **kwargs) -> dict:
    return get_runtime().get_stats(*args, **kwargs)


def get_llm_client():
    return get_runtime().get_llm_client()


def get_config_dir() -> Path:
    return get_runtime().config_dir