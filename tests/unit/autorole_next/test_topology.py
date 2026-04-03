from __future__ import annotations

from autorole_next.store import AutoRoleStoreAdapter
from autorole_next.stage_ids import PACKAGING, SCORING, TAILORING
from autorole_next.topology import build_topology


def test_build_topology_exposes_scoring_tailoring_packaging_stages(tmp_path) -> None:
    store = AutoRoleStoreAdapter(str(tmp_path / "autorole-next.db"))
    topology = build_topology(store)

    assert [stage.id for stage in topology.stages] == [SCORING, TAILORING, PACKAGING]
    assert topology.default_dlq == "global_dlq"