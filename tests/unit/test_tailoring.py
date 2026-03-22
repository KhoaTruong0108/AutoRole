from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from autorole.config import TailoringConfig
from autorole.context import DiffReport, JobApplicationContext, ScoreReport, TailoredResume
from autorole.stages.tailoring import TailoringStage, _select_degree
from tests.conftest import SAMPLE_LISTING

try:
	from pipeline.types import Message
except Exception:
	class Message:  # pragma: no cover - fallback when pipeline package is unavailable
		def __init__(self, run_id: str, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
			self.run_id = run_id
			self.payload = payload
			self.metadata = metadata or {}


class MockLLM:
	def __init__(self, output: str) -> None:
		self.output = output

	async def call(self, **_kwargs: Any) -> str:
		return self.output


def _score(overall: float) -> ScoreReport:
	return ScoreReport(
		resume_id="master",
		jd_html="<html><body>jd</body></html>",
		jd_breakdown={"required_skills": ["python", "kubernetes"]},
		overall_score=overall,
		criteria_scores={
			"technical_skills": 0.5,
			"experience_depth": 0.5,
			"seniority_alignment": 0.5,
			"domain_relevance": 0.5,
			"culture_fit": 0.5,
		},
		matched=["technical_skills"],
		mismatched=["domain_relevance"],
		scored_at=datetime.now(timezone.utc),
	)


@pytest.mark.parametrize(
	"score,expected",
	[(0.88, 0), (0.75, 1), (0.60, 2), (0.45, 3), (0.30, 4)],
)
def test_tailoring_selects_correct_degree_per_score(score: float, expected: int) -> None:
	cfg = TailoringConfig(degree_4_enabled=True)
	assert _select_degree(score, cfg) == expected


def test_tailoring_blocks_when_score_too_low_and_degree4_disabled() -> None:
	cfg = TailoringConfig(degree_4_enabled=False)
	assert _select_degree(0.30, cfg) is None


async def test_tailoring_writes_md_file_to_disk(test_config: Any) -> None:
	stage = TailoringStage(test_config, MockLLM("# Tailored\n\n- Added Python and Kubernetes\n"))
	ctx = JobApplicationContext(run_id="acme_3847291", listing=SAMPLE_LISTING, score=_score(0.75))

	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.tailored is not None
	assert Path(out_ctx.tailored.file_path).exists()


async def test_tailoring_increments_version_on_loop(test_config: Any, tmp_path: Path) -> None:
	v1 = tmp_path / "acme_cloud_3847291_v1_deadbeef.md"
	v1.write_text("# Tailored v1\n", encoding="utf-8")

	ctx = JobApplicationContext(
		run_id="acme_3847291",
		listing=SAMPLE_LISTING,
		score=_score(0.75),
		tailored=TailoredResume(
			resume_id="deadbeef-0000-0000-0000-000000000000",
			parent_resume_id="master",
			tailoring_degree=1,
			file_path=str(v1),
			diff_summary="{}",
			tailored_at=datetime.now(timezone.utc),
		),
	)

	stage = TailoringStage(test_config, MockLLM("# Tailored v2\n"))
	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.tailored is not None
	assert "_v2_" in Path(out_ctx.tailored.file_path).name


async def test_tailoring_produces_diff_report(test_config: Any) -> None:
	stage = TailoringStage(
		test_config,
		MockLLM("# Test Resume\n\nExperience placeholder\n\nAdded Kubernetes ownership\n"),
	)
	ctx = JobApplicationContext(run_id="acme_3847291", listing=SAMPLE_LISTING, score=_score(0.75))

	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.tailored is not None

	parsed = json.loads(out_ctx.tailored.diff_summary)
	assert "tailoring_degree" in parsed
	assert "sections" in parsed
	DiffReport.model_validate(parsed)
