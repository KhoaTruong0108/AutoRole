from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from autorole.config import AppConfig, ScoringWeights
from autorole.context import JobApplicationContext, ScoreReport
from autorole.integrations.llm import LLMClient, LLMResponseError
from autorole.integrations.scrapers import get_scraper
from autorole.stage_base import AutoRoleStage

try:
	from pipeline.interfaces import Stage
	from pipeline.types import Message, StageResult
except Exception:
	class Stage:
		async def execute(self, message: "Message") -> "StageResult":
			raise NotImplementedError

	class Message:
		def __init__(self, run_id: str, payload: Any, metadata: dict[str, Any] | None = None) -> None:
			self.run_id = run_id
			self.payload = payload
			self.metadata = metadata or {}

	class StageResult:
		def __init__(
			self,
			success: bool,
			output: Any = None,
			error: str | None = None,
			error_type: str | None = None,
		) -> None:
			self.success = success
			self.output = output
			self.error = error
			self.error_type = error_type

		@classmethod
		def ok(cls, output: Any) -> "StageResult":
			return cls(success=True, output=output)

		@classmethod
		def fail(cls, error: str, error_type: str = "") -> "StageResult":
			return cls(success=False, error=error, error_type=error_type)


JD_PARSE_SYSTEM_PROMPT = """You are a job description parser.
Return strict JSON that conforms to this schema:
{
  qualifications: list[str],
  responsibilities: list[str],
  required_skills: list[str],
  preferred_skills: list[str],
  culture_signals: list[str]
}
Only include concrete evidence found in the JD text.
"""


SCORING_SYSTEM_PROMPT = """You are a resume-vs-job scoring engine.
Evaluate the resume against the JD and return strict JSON that conforms to:
{
  scores: {
    technical_skills: float,
    experience_depth: float,
    seniority_alignment: float,
    domain_relevance: float,
    culture_fit: float
  },
  details: {
    technical_skills: {score: float, matched: list[str], gaps: list[str]},
    experience_depth: {score: float, matched: list[str], gaps: list[str]},
    seniority_alignment: {score: float, matched: list[str], gaps: list[str]},
    domain_relevance: {score: float, matched: list[str], gaps: list[str]},
    culture_fit: {score: float, matched: list[str], gaps: list[str]}
  }
}
All scores must be between 0 and 1 inclusive.
"""


class JDBreakdown(BaseModel):
	qualifications: list[str] = Field(default_factory=list)
	responsibilities: list[str] = Field(default_factory=list)
	required_skills: list[str] = Field(default_factory=list)
	preferred_skills: list[str] = Field(default_factory=list)
	culture_signals: list[str] = Field(default_factory=list)


class CriterionDetail(BaseModel):
	score: float
	matched: list[str] = Field(default_factory=list)
	gaps: list[str] = Field(default_factory=list)


class CriterionScores(BaseModel):
	scores: dict[str, float]
	details: dict[str, CriterionDetail]


class ScoringStage(Stage):
	name = "scoring"
	concurrency = 3

	def __init__(self, config: AppConfig, llm_client: LLMClient, page: Any) -> None:
		self._config = config
		self._llm = llm_client
		self._page = page

	async def execute(self, message: Message) -> StageResult:
		ctx = JobApplicationContext.model_validate(message.payload)
		if ctx.listing is None:
			return StageResult.fail("ScoringStage: ctx.listing is None", "PreconditionError")

		if ctx.score is not None and ctx.score.jd_html:
			jd_html = ctx.score.jd_html
		else:
			try:
				jd_html = await _fetch_jd_html(self._page, ctx.listing.job_url)
			except Exception as exc:
				return StageResult.fail(f"JD fetch failed: {exc}", "FetchError")

		if ctx.tailored is not None:
			resume_path = Path(ctx.tailored.file_path)
			resume_id = ctx.tailored.resume_id
		else:
			resume_path = Path(self._config.master_resume).expanduser()
			resume_id = "master"

		try:
			resume_md = resume_path.read_text(encoding="utf-8")
		except Exception as exc:
			return StageResult.fail(f"Resume read failed: {exc}", "ResumeReadError")

		try:
			jd_breakdown = await self._llm.call(
				system=JD_PARSE_SYSTEM_PROMPT,
				user=f"Parse this job description text:\n\n{_extract_text(jd_html)}",
				response_model=JDBreakdown,
			)
			criterion_scores = await self._llm.call(
				system=SCORING_SYSTEM_PROMPT,
				user=_build_scoring_prompt(jd_breakdown, resume_md),
				response_model=CriterionScores,
			)
		except LLMResponseError as exc:
			return StageResult.fail(str(exc), "LLMResponseError")
		except Exception as exc:
			return StageResult.fail(f"Scoring call failed: {exc}", "ScoringError")

		overall = compute_overall_score(criterion_scores.scores, self._config.scoring_weights)
		matched: list[str] = []
		mismatched: list[str] = []
		for criterion, detail in criterion_scores.details.items():
			if detail.score >= 0.7:
				matched.append(criterion)
			else:
				mismatched.append(criterion)

		score = ScoreReport(
			resume_id=resume_id,
			jd_html=jd_html,
			jd_breakdown=jd_breakdown.model_dump(),
			overall_score=overall,
			criteria_scores=criterion_scores.scores,
			matched=matched,
			mismatched=mismatched,
			scored_at=datetime.now(timezone.utc),
		)

		updated_ctx = ctx.model_copy(update={"score": score})
		return StageResult.ok(updated_ctx)


async def _fetch_jd_html(page: Any, url: str) -> str:
	scraper = get_scraper(url, page=page)
	jd = await scraper.fetch_job_description(url)
	if jd.raw_html:
		return jd.raw_html
	# Safety fallback if a scraper returns empty HTML unexpectedly.
	await page.goto(url, wait_until="networkidle", timeout=30_000)
	return await page.content()


def _extract_text(html: str) -> str:
	soup = BeautifulSoup(html, "html.parser")
	return soup.get_text(separator="\n", strip=True)


def _build_scoring_prompt(jd_breakdown: JDBreakdown, resume_md: str) -> str:
	return (
		"Job description breakdown (JSON):\n"
		f"{jd_breakdown.model_dump_json(indent=2)}\n\n"
		"Resume markdown:\n"
		f"{resume_md}\n\n"
		"Score the resume across all required criteria and return JSON only."
	)


def compute_overall_score(
	criterion_scores: dict[str, float],
	weights: ScoringWeights,
) -> float:
	normalised = weights.normalised()
	total = 0.0
	for key, weight in normalised.items():
		total += float(criterion_scores.get(key, 0.0)) * weight
	return total


class ScoringExecutor(AutoRoleStage):
	name = "scoring"

	async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
		run_id = ctx.run_id
		score = ctx.score
		if score is None:
			return

		self._write_artifact(
			f"attempt_{attempt}_summary.json",
			json.dumps(
				{
					"overall_score": score.overall_score,
					"criteria_scores": score.criteria_scores,
					"matched": score.matched,
					"mismatched": score.mismatched,
					"jd_breakdown": score.jd_breakdown,
				},
				indent=2,
				ensure_ascii=False,
			)
			+ "\n",
			run_id,
		)
		self._write_artifact(
			f"attempt_{attempt}_job_description.html",
			score.jd_html,
			run_id,
		)
		criteria_md = [
			f"# Scoring Criteria (attempt {attempt})",
			"",
			f"Overall score: {score.overall_score:.3f}",
			"",
			"## Criteria Scores",
			"",
			json.dumps(score.criteria_scores, indent=2, ensure_ascii=False),
			"",
			"## Job Requirements Breakdown",
			"",
			json.dumps(score.jd_breakdown, indent=2, ensure_ascii=False),
			"",
			"## Matched",
			"",
			json.dumps(score.matched, indent=2, ensure_ascii=False),
			"",
			"## Mismatched",
			"",
			json.dumps(score.mismatched, indent=2, ensure_ascii=False),
			"",
		]
		self._write_artifact(
			f"attempt_{attempt}_criteria.md",
			"\n".join(criteria_md),
			run_id,
		)
		await self._repo.upsert_score(run_id, score, attempt=attempt)

	async def on_failure(
		self,
		ctx: JobApplicationContext,
		result: Any,
		attempt: int,
	) -> JobApplicationContext | None:
		print(f"[fail] {self.name}: {result.error}")
		self._write_artifact(
			f"attempt_{attempt}_error.txt",
			f"error_type={getattr(result, 'error_type', '')}\nerror={result.error}\n",
			ctx.run_id,
		)
		from autorole.stage_base import _emit_resume_hint

		_emit_resume_hint(self._logger, ctx.run_id, self._mode, self.name)
		return None

	def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
		if ctx.score is None:
			return
		print(f"[ok] scoring -> overall_score={ctx.score.overall_score:.3f} (attempt {attempt})")
