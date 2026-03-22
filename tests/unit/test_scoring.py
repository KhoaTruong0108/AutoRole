from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autorole.config import ScoringWeights
from autorole.context import JobApplicationContext, ScoreReport, TailoredResume
from autorole.integrations.llm import LLMResponseError
from autorole.stages.scoring import (
	CriterionDetail,
	CriterionScores,
	JDBreakdown,
	ScoringStage,
)
from tests.conftest import SAMPLE_LISTING

try:
	from pipeline.types import Message
except Exception:
	class Message:  # pragma: no cover - fallback when pipeline package is unavailable
		def __init__(self, run_id: str, payload: dict[str, Any]) -> None:
			self.run_id = run_id
			self.payload = payload


class MockPage:
	def __init__(self, html: str, should_fail: bool = False) -> None:
		self._html = html
		self._should_fail = should_fail
		self.navigation_count = 0

	async def goto(self, _url: str, **_kwargs: Any) -> None:
		self.navigation_count += 1
		if self._should_fail:
			raise RuntimeError("network error")

	async def content(self) -> str:
		return self._html


class MockLLMClient:
	def __init__(self, jd_breakdown: JDBreakdown, criterion_scores: CriterionScores) -> None:
		self.jd_breakdown = jd_breakdown
		self.criterion_scores = criterion_scores
		self.calls: list[dict[str, Any]] = []

	async def call(self, **kwargs: Any) -> Any:
		self.calls.append(kwargs)
		response_model = kwargs.get("response_model")
		if response_model is JDBreakdown:
			return self.jd_breakdown
		if response_model is CriterionScores:
			return self.criterion_scores
		raise AssertionError("Unexpected response_model")


class ErrorLLMClient:
	async def call(self, **_kwargs: Any) -> Any:
		raise LLMResponseError("bad llm response")


def _criterion_scores(technical: float = 0.8) -> CriterionScores:
	scores = {
		"technical_skills": technical,
		"experience_depth": 0.6,
		"seniority_alignment": 0.7,
		"domain_relevance": 0.5,
		"culture_fit": 0.9,
	}
	return CriterionScores(
		scores=scores,
		details={
			key: CriterionDetail(
				score=value,
				matched=[f"matched-{key}"],
				gaps=[f"gap-{key}"],
			)
			for key, value in scores.items()
		},
	)


def _jd_breakdown() -> JDBreakdown:
	return JDBreakdown(
		qualifications=["5+ years Python"],
		responsibilities=["Build APIs"],
		required_skills=["Python", "PostgreSQL"],
		preferred_skills=["Kubernetes"],
		culture_signals=["ownership"],
	)


async def test_scoring_fetches_jd_on_first_pass(test_config: Any) -> None:
	page = MockPage("<html><body><h1>JD</h1></body></html>")
	llm = MockLLMClient(_jd_breakdown(), _criterion_scores())
	stage = ScoringStage(test_config, llm, page)

	ctx = JobApplicationContext(run_id="acme_123", listing=SAMPLE_LISTING)
	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert page.navigation_count == 1
	assert out_ctx.score is not None
	assert out_ctx.score.jd_html


async def test_scoring_reuses_jd_html_on_loop_reentry(test_config: Any) -> None:
	page = MockPage("<html><body>SHOULD_NOT_BE_USED</body></html>")
	llm = MockLLMClient(_jd_breakdown(), _criterion_scores())
	stage = ScoringStage(test_config, llm, page)

	ctx = JobApplicationContext(
		run_id="acme_123",
		listing=SAMPLE_LISTING,
		score=ScoreReport(
			resume_id="master",
			jd_html="CACHED",
			jd_breakdown={},
			overall_score=0.5,
			criteria_scores={},
			matched=[],
			mismatched=[],
			scored_at=datetime.now(timezone.utc),
		),
	)

	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert page.navigation_count == 0
	assert out_ctx.score is not None
	assert out_ctx.score.jd_html == "CACHED"


async def test_scoring_uses_tailored_resume_on_loop_reentry(test_config: Any, tmp_path: Path) -> None:
	tailored_path = tmp_path / "tailored.md"
	tailored_path.write_text("TAILORED-RESUME-CONTENT", encoding="utf-8")

	page = MockPage("<html><body>JD</body></html>")
	llm = MockLLMClient(_jd_breakdown(), _criterion_scores())
	stage = ScoringStage(test_config, llm, page)

	ctx = JobApplicationContext(
		run_id="acme_123",
		listing=SAMPLE_LISTING,
		tailored=TailoredResume(
			resume_id="tailored_1",
			parent_resume_id="master",
			tailoring_degree=1,
			file_path=str(tailored_path),
			diff_summary="{}",
			tailored_at=datetime.now(timezone.utc),
		),
	)

	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))

	assert result.success
	assert len(llm.calls) == 2
	assert "TAILORED-RESUME-CONTENT" in llm.calls[1]["user"]


async def test_scoring_populates_all_score_fields(test_config: Any) -> None:
	page = MockPage("<html><body>JD</body></html>")
	llm = MockLLMClient(_jd_breakdown(), _criterion_scores())
	stage = ScoringStage(test_config, llm, page)

	ctx = JobApplicationContext(run_id="acme_123", listing=SAMPLE_LISTING)
	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.score is not None
	assert 0.0 <= out_ctx.score.overall_score <= 1.0
	assert set(out_ctx.score.criteria_scores.keys()) == {
		"technical_skills",
		"experience_depth",
		"seniority_alignment",
		"domain_relevance",
		"culture_fit",
	}
	assert out_ctx.score.matched
	assert out_ctx.score.mismatched


async def test_scoring_weighted_score_calculation(test_config: Any) -> None:
	cfg = test_config.model_copy(
		update={
			"scoring_weights": ScoringWeights(
				technical_skills=1.0,
				experience_depth=0.0,
				seniority_alignment=0.0,
				domain_relevance=0.0,
				culture_fit=0.0,
			)
		}
	)
	page = MockPage("<html><body>JD</body></html>")
	llm = MockLLMClient(_jd_breakdown(), _criterion_scores(technical=0.42))
	stage = ScoringStage(cfg, llm, page)

	ctx = JobApplicationContext(run_id="acme_123", listing=SAMPLE_LISTING)
	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.score is not None
	assert out_ctx.score.overall_score == 0.42


async def test_scoring_fails_on_jd_fetch_error(test_config: Any) -> None:
	page = MockPage("", should_fail=True)
	llm = MockLLMClient(_jd_breakdown(), _criterion_scores())
	stage = ScoringStage(test_config, llm, page)

	ctx = JobApplicationContext(run_id="acme_123", listing=SAMPLE_LISTING)
	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))

	assert not result.success
	assert result.error_type == "FetchError"


async def test_scoring_fails_on_llm_error(test_config: Any) -> None:
	page = MockPage("<html><body>JD</body></html>")
	stage = ScoringStage(test_config, ErrorLLMClient(), page)

	ctx = JobApplicationContext(run_id="acme_123", listing=SAMPLE_LISTING)
	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))

	assert not result.success
	assert result.error_type == "LLMResponseError"
