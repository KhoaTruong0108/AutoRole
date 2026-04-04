from __future__ import annotations

from autorole_next.store import AutoRoleStoreAdapter
from autorole_next.stage_ids import CONCLUDING, FIELD_COMPLETER, FORM_SCRAPER, FORM_SUBMISSION, PACKAGING, SCORING, SESSION, TAILORING
from autorole_next.topology import build_topology


def test_build_topology_exposes_scoring_tailoring_packaging_session_form_scraper_field_completer_form_submission_concluding_stages(tmp_path) -> None:
    store = AutoRoleStoreAdapter(str(tmp_path / "autorole-next.db"))
    topology = build_topology(store)

    assert [stage.id for stage in topology.stages] == [SCORING, TAILORING, PACKAGING, SESSION, FORM_SCRAPER, FIELD_COMPLETER, FORM_SUBMISSION, CONCLUDING]
    assert topology.default_dlq == "global_dlq"