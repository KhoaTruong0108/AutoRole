from __future__ import annotations

import pytest

from autorole_next._snapflow import BlockedError, StateContext
from autorole_next.gates.scoring import ScoringGate


def _ctx(score: float, *, attempt: int = 0) -> StateContext[dict[str, object]]:
    return StateContext[dict[str, object]](
        correlation_id="corr-1",
        current_stage="scoring",
        attempt=attempt,
        data={
            "scoring": {
                "attempt": attempt + 1,
                "overall_score": score,
                "criteria_scores": {},
                "matched": [],
                "mismatched": [],
                "jd_summary": "fixture",
            }
        },
    )


def test_scoring_gate_always_routes_to_tailoring_on_valid_payload() -> None:
    gate = ScoringGate()
    assert gate.evaluate(_ctx(0.92)) == "tailoring"
    assert gate.evaluate(_ctx(0.10, attempt=2)) == "tailoring"


def test_scoring_gate_blocks_when_scoring_payload_missing() -> None:
    gate = ScoringGate()
    ctx = StateContext[dict[str, object]](
        correlation_id="corr-1",
        current_stage="scoring",
        attempt=0,
        data={},
    )
    with pytest.raises(BlockedError):
        gate.evaluate(ctx)