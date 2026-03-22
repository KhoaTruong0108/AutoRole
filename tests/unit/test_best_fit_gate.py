from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from autorole.context import JobApplicationContext, ScoreReport, TailoredResume
from autorole.gates.best_fit import BestFitGate

try:
	from pipeline.types import Message, StageResult
except Exception:
	from autorole.gates.best_fit import Message, StageResult


def _ctx(score: float, degree: int) -> JobApplicationContext:
	return JobApplicationContext(
		run_id="acme_123",
		score=ScoreReport(
			resume_id="r1",
			jd_html="",
			jd_breakdown={},
			overall_score=score,
			criteria_scores={},
			matched=[],
			mismatched=[],
			scored_at=datetime.now(timezone.utc),
		),
		tailored=TailoredResume(
			resume_id="t1",
			parent_resume_id="master",
			tailoring_degree=degree,
			file_path="/tmp/t1.md",
			diff_summary="{}",
			tailored_at=datetime.now(timezone.utc),
		),
	)


def _decision_value(decision: Any) -> str:
	return getattr(decision, "value", str(decision))


def test_best_fit_gate_passes_degree_zero() -> None:
	gate = BestFitGate(max_attempts=2)
	r = gate.evaluate(
		StageResult.ok(_ctx(0.88, 0).model_dump()),
		Message(run_id="r", payload={}, attempt=1, metadata={}),
	)
	assert _decision_value(r.decision) == "pass"


def test_best_fit_gate_loops_on_first_attempt_with_baseline() -> None:
	gate = BestFitGate(max_attempts=2)
	r = gate.evaluate(
		StageResult.ok(_ctx(0.72, 1).model_dump()),
		Message(run_id="r", payload={}, attempt=1, metadata={}),
	)
	assert _decision_value(r.decision) == "loop"
	assert r.loop_target == "scoring"
	assert "baseline=0.7200" in r.reason


def test_best_fit_gate_loops_on_improvement() -> None:
	gate = BestFitGate(max_attempts=3)
	r = gate.evaluate(
		StageResult.ok(_ctx(0.80, 1).model_dump()),
		Message(run_id="r", payload={}, attempt=2, metadata={"last_score_before_tailoring": 0.72}),
	)
	assert _decision_value(r.decision) == "loop"
	assert "score_improved" in r.reason


def test_best_fit_gate_blocks_on_stagnation() -> None:
	gate = BestFitGate(max_attempts=3)
	r = gate.evaluate(
		StageResult.ok(_ctx(0.72, 1).model_dump()),
		Message(run_id="r", payload={}, attempt=2, metadata={"last_score_before_tailoring": 0.72}),
	)
	assert _decision_value(r.decision) == "block"
	assert "stagnated" in r.reason


def test_best_fit_gate_blocks_on_regression() -> None:
	gate = BestFitGate(max_attempts=3)
	r = gate.evaluate(
		StageResult.ok(_ctx(0.69, 1).model_dump()),
		Message(run_id="r", payload={}, attempt=2, metadata={"last_score_before_tailoring": 0.72}),
	)
	assert _decision_value(r.decision) == "block"
	assert "regressed" in r.reason


def test_best_fit_gate_blocks_at_max_attempts() -> None:
	gate = BestFitGate(max_attempts=2)
	r = gate.evaluate(
		StageResult.ok(_ctx(0.80, 1).model_dump()),
		Message(run_id="r", payload={}, attempt=2, metadata={"last_score_before_tailoring": 0.72}),
	)
	assert _decision_value(r.decision) == "block"
	assert "Max tailoring attempts" in r.reason
