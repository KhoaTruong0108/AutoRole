from __future__ import annotations

from types import SimpleNamespace

from autorole.context import FormSession, JobApplicationContext
from autorole.gates.form_page import FormPageGate
from autorole.integrations.form_controls.models import DetectionResult


def _ctx(action: str) -> JobApplicationContext:
	return JobApplicationContext(
		run_id="run-1",
		form_session=FormSession(
			detection=DetectionResult(
				run_id="run-1",
				platform_id="workday",
				apply_url="https://example.com/apply",
				used_iframe=False,
				detection_method="url",
			),
			page_index=2,
			last_advance_action=action,
		),
	)


def test_form_page_gate_loops_on_next_page() -> None:
	gate = FormPageGate()
	result = gate.evaluate(SimpleNamespace(output=_ctx("next_page").model_dump()), SimpleNamespace())
	assert getattr(result.decision, "value", str(result.decision)) == "loop"


def test_form_page_gate_passes_on_submit() -> None:
	gate = FormPageGate()
	result = gate.evaluate(SimpleNamespace(output=_ctx("submit").model_dump()), SimpleNamespace())
	assert getattr(result.decision, "value", str(result.decision)) == "pass"


def test_form_page_gate_blocks_without_session() -> None:
	gate = FormPageGate()
	result = gate.evaluate(SimpleNamespace(output=JobApplicationContext(run_id="run-1").model_dump()), SimpleNamespace())
	assert getattr(result.decision, "value", str(result.decision)) == "block"
