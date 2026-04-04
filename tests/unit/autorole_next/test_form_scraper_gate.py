from __future__ import annotations

from autorole_next._snapflow import StateContext
from autorole_next.gates.form_scraper import FormScraperGate


def test_form_scraper_gate_routes_to_field_completer() -> None:
    gate = FormScraperGate()
    ctx = StateContext[dict[str, object]](
        correlation_id="corr-1",
        current_stage="formScraper",
        data={"formScraper": {"page_index": 0}},
    )

    assert gate.evaluate(ctx) == "fieldCompleter"
