from __future__ import annotations

import pytest

from autorole_next._snapflow import BlockedError, StateContext
from autorole_next.gates.form_submission import FormSubmissionGate


def _ctx(payload: dict[str, object]) -> StateContext[dict[str, object]]:
    return StateContext[dict[str, object]](
        correlation_id="corr-1",
        current_stage="form_submission",
        data={"form_submission": payload},
    )


def test_form_submission_gate_passes_on_pass_decision() -> None:
    gate = FormSubmissionGate(max_loops=2)
    assert gate.evaluate(_ctx({"decision": "pass"})) == "concluding"


def test_form_submission_gate_loops_back_to_form_scraper() -> None:
    gate = FormSubmissionGate(max_loops=2)
    assert gate.evaluate(_ctx({"decision": "loop", "loop_count": 1})) == "formScraper"


def test_form_submission_gate_blocks_on_block_decision() -> None:
    gate = FormSubmissionGate(max_loops=2)
    with pytest.raises(BlockedError):
        gate.evaluate(_ctx({"decision": "block", "reason": "guardrail"}))
