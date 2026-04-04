from __future__ import annotations

from autorole_next._snapflow import StateContext
from autorole_next.gates.field_completer import FieldCompleterGate


def test_field_completer_gate_routes_to_form_submission() -> None:
    gate = FieldCompleterGate()
    ctx = StateContext[dict[str, object]](
        correlation_id="corr-1",
        current_stage="fieldCompleter",
        data={"fieldCompleter": {"fill_instructions": []}},
    )

    assert gate.evaluate(ctx) == "formSubmission"
