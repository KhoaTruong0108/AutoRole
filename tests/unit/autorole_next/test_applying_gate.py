from __future__ import annotations

from autorole_next._snapflow import StateContext
from autorole_next.gates.applying import ApplyingGate


def test_applying_gate_routes_to_concluding() -> None:
    gate = ApplyingGate()
    ctx = StateContext[dict[str, object]](
        correlation_id="corr-1",
        current_stage="llm_applying",
        data={"llm_applying": {"status": "applied"}},
    )

    assert gate.evaluate(ctx) == "concluding"
