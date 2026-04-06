from __future__ import annotations

from autorole_next._snapflow import StateContext
from autorole_next.gates.packaging import PackagingGate


def test_packaging_gate_routes_to_session() -> None:
    gate = PackagingGate()
    ctx = StateContext[dict[str, object]](
        correlation_id="corr-1",
        current_stage="packaging",
        data={"packaging": {"status": "ready"}},
    )

    assert gate.evaluate(ctx) == "llm_applying"


def test_packaging_gate_routes_to_llm_applying_when_apply_mode_requests_it() -> None:
    gate = PackagingGate()
    ctx = StateContext[dict[str, object]](
        correlation_id="corr-1",
        current_stage="packaging",
        data={"packaging": {"status": "ready"}},
        metadata={"apply_mode": "llm_applying"},
    )

    assert gate.evaluate(ctx) == "llm_applying"
