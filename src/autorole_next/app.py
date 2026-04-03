from __future__ import annotations

from pathlib import Path
from typing import Any

from ._snapflow import PipelineRunner
from .store import AutoRoleStoreAdapter
from .topology import build_topology


def _resolve_db_path(config_or_db_path: Any) -> str:
    if isinstance(config_or_db_path, str):
        return config_or_db_path
    if isinstance(config_or_db_path, Path):
        return str(config_or_db_path)
    db_path = getattr(config_or_db_path, "db_path", None)
    if isinstance(db_path, Path):
        return str(db_path)
    if isinstance(db_path, str):
        return db_path
    raise TypeError("build_store/build_runner require a db path or an object with a db_path attribute")


def build_store(config_or_db_path: Any) -> AutoRoleStoreAdapter:
    return AutoRoleStoreAdapter(_resolve_db_path(config_or_db_path))


def build_runner(config_or_db_path: Any) -> PipelineRunner:
    store = build_store(config_or_db_path)
    topology = build_topology(store)
    return PipelineRunner(topology)


__all__ = ["build_runner", "build_store", "build_topology"]