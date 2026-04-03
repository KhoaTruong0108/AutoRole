from __future__ import annotations

import sys
from pathlib import Path


def _prefer_workspace_snapflow() -> None:
    workspace_snapflow_src = Path(__file__).resolve().parents[3] / "SnapFlow" / "src"
    if workspace_snapflow_src.exists():
        workspace_path = str(workspace_snapflow_src)
        if workspace_path in sys.path:
            sys.path.remove(workspace_path)
        sys.path.insert(0, workspace_path)


_prefer_workspace_snapflow()

from snapflow import (  # noqa: E402
    BlockedError,
    ErrorCategory,
    Executor,
    PipelineRunner,
    PipelineSeeder,
    Gate,
    RunStatus,
    SQLiteQueueAdapter,
    SQLiteStoreAdapter,
    StageNode,
    StageResult,
    StateContext,
    Topology,
)

__all__ = [
    "BlockedError",
    "ErrorCategory",
    "Executor",
    "Gate",
    "PipelineRunner",
    "PipelineSeeder",
    "RunStatus",
    "SQLiteQueueAdapter",
    "SQLiteStoreAdapter",
    "StageNode",
    "StageResult",
    "StateContext",
    "Topology",
]