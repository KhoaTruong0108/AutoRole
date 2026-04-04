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

    assert gate.evaluate(ctx) == "session"
