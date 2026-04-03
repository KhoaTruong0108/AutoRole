from __future__ import annotations

import pytest

from autorole_next._snapflow import BlockedError
from autorole_next._snapflow import StateContext
from autorole_next.gates.tailoring import TailoringGate


def _ctx(*, degree: int, score_attempt: int = 1) -> StateContext[dict[str, object]]:
    return StateContext[dict[str, object]](
        correlation_id="corr-1",
        current_stage="tailoring",
        data={
            "scoring": {"attempt": score_attempt, "overall_score": 0.5},
            "tailoring": {"tailoring_degree": degree},
        },
    )


def test_tailoring_gate_routes_to_packaging_for_zero_degree() -> None:
    gate = TailoringGate()
    assert gate.evaluate(_ctx(degree=0, score_attempt=1)) == "packaging"


def test_tailoring_gate_routes_back_to_scoring_for_non_zero_degree() -> None:
    gate = TailoringGate(max_attempts=3)
    assert gate.evaluate(_ctx(degree=2, score_attempt=2)) == "scoring"


def test_tailoring_gate_blocks_on_max_attempts_for_non_zero_degree() -> None:
    gate = TailoringGate(max_attempts=3)
    with pytest.raises(BlockedError):
        gate.evaluate(_ctx(degree=1, score_attempt=3))