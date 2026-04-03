from __future__ import annotations

from ._snapflow import SQLiteQueueAdapter, StageNode, Topology
from .executors.packaging import PackagingExecutor
from .executors.scoring import ScoringExecutor
from .executors.tailoring import TailoringExecutor
from .gates.scoring import ScoringGate
from .gates.tailoring import TailoringGate
from .stage_ids import PACKAGING, SCORING, TAILORING
from .store import AutoRoleStoreAdapter


def build_topology(store: AutoRoleStoreAdapter) -> Topology:
    ScoringExecutor.configure_store(store)
    TailoringExecutor.configure_store(store)
    return Topology(
        stages=[
            StageNode(
                id=SCORING,
                executor=ScoringExecutor,
                gate=ScoringGate(),
            ),
            StageNode(
                id=TAILORING,
                executor=TailoringExecutor,
                gate=TailoringGate(),
            ),
            StageNode(
                id=PACKAGING,
                executor=PackagingExecutor,
            ),
        ],
        queue_backend=SQLiteQueueAdapter(store.path),
        store_backend=store,
    )
