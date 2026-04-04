from __future__ import annotations

from autorole_next._snapflow import StateContext
from autorole_next.gates.session import SessionGate


def test_session_gate_routes_to_form_scraper() -> None:
    gate = SessionGate()
    ctx = StateContext[dict[str, object]](
        correlation_id="corr-1",
        current_stage="session",
        data={"session": {"authenticated": False}},
    )

    assert gate.evaluate(ctx) == "formScraper"
