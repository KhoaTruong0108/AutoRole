from __future__ import annotations

import difflib
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from autorole.config import AppConfig, TailoringConfig
from autorole.context import DiffChange, DiffReport, DiffSection, JobApplicationContext, TailoredResume
from autorole.integrations.llm import LLMClient, LLMResponseError

try:
	from pipeline.interfaces import Stage
	from pipeline.types import Message, StageResult
except Exception:
	class Stage:
		async def execute(self, message: "Message") -> "StageResult":
			raise NotImplementedError

	class Message:
		def __init__(self, run_id: str, payload: object, metadata: dict | None = None) -> None:
			self.run_id = run_id
			self.payload = payload
			self.metadata = metadata or {}

	class StageResult:
		def __init__(self, success: bool, output: object = None, error: str | None = None, error_type: str | None = None) -> None:
			self.success = success
			self.output = output
			self.error = error
			self.error_type = error_type

		@classmethod
		def ok(cls, output: object) -> "StageResult":
			return cls(success=True, output=output)

		@classmethod
		def fail(cls, error: str, error_type: str = "") -> "StageResult":
			return cls(success=False, error=error, error_type=error_type)


_TAILORING_SYSTEM_PROMPTS: dict[int, str] = {
	1: (
		"You are tailoring a resume with degree 1 (emphasis). "
		"Reorder and reword existing evidence only. Do not invent projects, employers, or timelines."
	),
	2: (
		"You are tailoring a resume with degree 2 (inflation). "
		"You may strengthen metrics and phrasing conservatively, but never alter company names or timeline facts."
	),
	3: (
		"You are tailoring a resume with degree 3 (projection). "
		"You may construct plausible project framing from real experience, while keeping company and timeline intact."
	),
	4: (
		"You are tailoring a resume with degree 4 (reinvention). "
		"This requires explicit user acknowledgement and should only be used as last resort. Keep contact info accurate."
	),
}


class TailoringStage(Stage):
	name = "tailoring"
	concurrency = 2

	def __init__(self, config: AppConfig, llm_client: LLMClient) -> None:
		self._config = config
		self._llm = llm_client

	async def execute(self, message: Message) -> StageResult:
		ctx = JobApplicationContext.model_validate(message.payload)
		if ctx.score is None:
			return StageResult.fail("TailoringStage: ctx.score is None", "PreconditionError")
		if ctx.listing is None:
			return StageResult.fail("TailoringStage: ctx.listing is None", "PreconditionError")

		degree = _select_degree(ctx.score.overall_score, self._config.tailoring)
		if degree is None:
			return StageResult.fail(
				(
					f"Score {ctx.score.overall_score:.2f} below all thresholds and "
					"degree_4_enabled=False. Skipping application."
				),
				"ScoreTooLow",
			)

		source_path = (
			Path(ctx.tailored.file_path)
			if ctx.tailored is not None
			else Path(self._config.master_resume).expanduser()
		)
		try:
			source_md = source_path.read_text(encoding="utf-8")
		except Exception as exc:
			return StageResult.fail(f"Source resume read failed: {exc}", "ResumeReadError")

		new_resume_id = str(uuid4())
		parent_id = ctx.tailored.resume_id if ctx.tailored is not None else "master"
		version = _next_version(ctx)
		out_path = _build_resume_path(self._config, ctx, new_resume_id, version)

		if degree == 0:
			tailored_md = source_md
			diff_report = DiffReport(tailoring_degree=0, overall_delta=0.0, sections=[])
		else:
			prompt = _build_tailoring_prompt(source_md, ctx.score, degree)
			try:
				response = await self._llm.call(
					system=_TAILORING_SYSTEM_PROMPTS[degree],
					user=prompt,
					response_model=None,
				)
			except LLMResponseError as exc:
				return StageResult.fail(str(exc), "LLMResponseError")
			except Exception as exc:
				return StageResult.fail(f"Tailoring call failed: {exc}", "TailoringError")

			tailored_md = str(response)
			diff_report = _compute_diff(source_md, tailored_md, ctx.score.jd_breakdown, degree)

		out_path.parent.mkdir(parents=True, exist_ok=True)
		out_path.write_text(tailored_md, encoding="utf-8")

		tailored = TailoredResume(
			resume_id=new_resume_id,
			parent_resume_id=parent_id,
			tailoring_degree=degree,
			file_path=str(out_path),
			diff_summary=diff_report.model_dump_json(),
			tailored_at=datetime.now(timezone.utc),
		)
		return StageResult.ok(ctx.model_copy(update={"tailored": tailored}))


def _select_degree(score: float, cfg: TailoringConfig) -> int | None:
	if score >= cfg.pass_threshold:
		return 0
	if score >= cfg.degree_1_threshold:
		return 1
	if score >= cfg.degree_2_threshold:
		return 2
	if score >= cfg.degree_3_threshold:
		return 3
	if cfg.degree_4_enabled:
		return 4
	return None


def _next_version(ctx: JobApplicationContext) -> int:
	if ctx.tailored is None:
		return 1
	match = re.search(r"_v(\d+)_", Path(ctx.tailored.file_path).name)
	if not match:
		return 2
	return int(match.group(1)) + 1


def _build_resume_path(config: AppConfig, ctx: JobApplicationContext, resume_id: str, version: int) -> Path:
	company = ctx.listing.company_name.lower().replace(" ", "_")
	job_id = ctx.listing.job_id
	base = Path(config.resume_dir).expanduser()
	return base / f"{company}_{job_id}_v{version}_{resume_id[:8]}.md"


def _build_tailoring_prompt(source_md: str, score: object, degree: int) -> str:
	return (
		f"Tailoring degree: {degree}\n"
		f"Current overall score: {score.overall_score:.4f}\n"
		f"Mismatched criteria: {', '.join(score.mismatched) if score.mismatched else 'none'}\n\n"
		"Source resume markdown:\n"
		f"{source_md}\n\n"
		"Return only the fully revised markdown resume."
	)


def _compute_diff(
	source_md: str,
	tailored_md: str,
	jd_breakdown: dict[str, object],
	degree: int,
) -> DiffReport:
	changes: list[DiffChange] = []
	for line in difflib.ndiff(source_md.splitlines(), tailored_md.splitlines()):
		if line.startswith("- "):
			text = line[2:].strip()
			if text:
				changes.append(
					DiffChange(
						location="Resume",
						criterion=_infer_criterion(text, jd_breakdown),
						change_type="removed",
						original=text,
						revised="",
						rationale="Removed while aligning resume to JD priorities",
					)
				)
		elif line.startswith("+ "):
			text = line[2:].strip()
			if text:
				changes.append(
					DiffChange(
						location="Resume",
						criterion=_infer_criterion(text, jd_breakdown),
						change_type="added",
						original="",
						revised=text,
						rationale="Added to better match JD language and requirements",
					)
				)

	section = DiffSection(
		section_name="Resume",
		changes=changes,
		net_impact=f"Captured {len(changes)} line-level diff change(s)",
	)
	return DiffReport(
		tailoring_degree=degree,
		overall_delta=0.0,
		sections=[section] if changes else [],
	)


def _infer_criterion(text: str, jd_breakdown: dict[str, object]) -> str:
	lower = text.lower()
	if any(word in lower for word in ["python", "kubernetes", "aws", "sql", "api", "backend"]):
		return "technical_skills"
	if any(word in lower for word in ["lead", "senior", "staff", "principal"]):
		return "seniority_alignment"
	if any(word in lower for word in ["year", "scale", "million", "complex", "production"]):
		return "experience_depth"
	if any(word in lower for word in ["fintech", "health", "saas", "domain", "industry"]):
		return "domain_relevance"

	serialized = str(jd_breakdown).lower()
	if "culture" in serialized or "collaboration" in serialized:
		return "culture_fit"
	return "technical_skills"
