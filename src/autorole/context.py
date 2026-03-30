from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pydantic
from pydantic import BaseModel, Field

from autorole.integrations.form_controls.models import (
	DetectionResult,
	ExecutionResult,
	ExtractedField,
	FieldOutcome,
	FillInstruction,
)

try:
	from pipeline.types import PipelineContext
except Exception:
	class PipelineContext(BaseModel):
		model_config = pydantic.ConfigDict(frozen=True)

		run_id: str
		started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class JobListing(BaseModel):
	job_url: str
	apply_url: str = ""
	company_name: str
	job_id: str
	job_title: str
	platform: str
	crawled_at: datetime


class ExplorationSeed(BaseModel):
	listing: JobListing
	source_name: str
	discovered_at: datetime
	source_metadata: dict[str, Any] = Field(default_factory=dict)


class ScoreReport(BaseModel):
	resume_id: str
	jd_html: str
	jd_breakdown: dict[str, Any]
	overall_score: float
	criteria_scores: dict[str, float]
	matched: list[str]
	mismatched: list[str]
	scored_at: datetime


class TailoredResume(BaseModel):
	resume_id: str
	parent_resume_id: str
	tailoring_degree: int
	file_path: str
	diff_summary: str
	tailored_at: datetime


class PackagedResume(BaseModel):
	resume_id: str
	pdf_path: str
	packaged_at: datetime


class SessionResult(BaseModel):
	platform: str
	authenticated: bool
	session_note: str
	established_at: datetime


class FormSession(BaseModel):
	detection: DetectionResult
	page_index: int = 0
	all_fields: list[ExtractedField] = Field(default_factory=list)
	all_instructions: list[FillInstruction] = Field(default_factory=list)
	all_outcomes: list[FieldOutcome] = Field(default_factory=list)
	last_advance_action: str = "next_page"
	screenshots: list[str] = Field(default_factory=list)


class FormIntelligenceResult(BaseModel):
	page_index: int = 0
	page_label: str = ""
	extracted_fields: list[ExtractedField] = Field(default_factory=list)
	fill_instructions: list[FillInstruction] = Field(default_factory=list)
	generated_at: datetime
	questionnaire: list[dict[str, Any]] = Field(default_factory=list)
	form_json_filled: dict[str, Any] = Field(default_factory=dict)


class LLMFieldCompletionResult(BaseModel):
	page_index: int = 0
	page_label: str = ""
	fill_instructions: list[FillInstruction] = Field(default_factory=list)
	generated_at: datetime
	questionnaire: list[dict[str, Any]] = Field(default_factory=list)
	form_json_filled: dict[str, Any] = Field(default_factory=dict)


class ApplicationResult(BaseModel):
	resume_id: str
	execution_result: ExecutionResult | None = None
	audit_log_path: str = ""
	applied_at: datetime
	questionnaire: list[dict[str, Any]] = Field(default_factory=list)
	form_json: dict[str, Any] = Field(default_factory=dict)
	fill_report: dict[str, Any] = Field(default_factory=dict)
	submission_status: str = ""
	submission_confirmed: bool = False


class DiffChange(BaseModel):
	location: str
	criterion: str
	change_type: str
	original: str
	revised: str
	rationale: str


class DiffSection(BaseModel):
	section_name: str
	changes: list[DiffChange]
	net_impact: str


class DiffReport(BaseModel):
	tailoring_degree: int
	overall_delta: float
	sections: list[DiffSection]
	total_changes: int = 0

	def model_post_init(self, __context: Any) -> None:
		object.__setattr__(self, "total_changes", sum(len(s.changes) for s in self.sections))

	def to_brief(self) -> str:
		lines = [f"Tailoring degree {self.tailoring_degree} | Δ score: {self.overall_delta:+.3f}"]
		for section in self.sections:
			by_criterion: dict[str, int] = {}
			for change in section.changes:
				by_criterion[change.criterion] = by_criterion.get(change.criterion, 0) + 1
			criteria_str = ", ".join(f"{k}:{v}" for k, v in by_criterion.items())
			lines.append(
				f"  [{section.section_name}] {len(section.changes)} change(s) -- {criteria_str}"
			)
			lines.append(f"    {section.net_impact}")
		return "\n".join(lines)

	def to_full(self) -> str:
		lines = [
			"# Diff Report",
			(
				f"Degree: {self.tailoring_degree} | Score delta: {self.overall_delta:+.3f} | "
				f"Total changes: {self.total_changes}"
			),
			"",
		]
		for section in self.sections:
			lines.append(f"## {section.section_name}")
			lines.append(f"_{section.net_impact}_")
			lines.append("")
			for idx, change in enumerate(section.changes, start=1):
				lines.extend(
					[
						f"### Change {idx}: {change.change_type} [{change.criterion}]",
						f"**Location:** {change.location}",
						f"**Rationale:** {change.rationale}",
						f"**Original:** {change.original or '(none)'}",
						f"**Revised:**  {change.revised or '(none)'}",
						"",
					]
				)
		return "\n".join(lines)


class JobApplicationContext(PipelineContext):
	listing: JobListing | None = None
	score: ScoreReport | None = None
	tailored: TailoredResume | None = None
	packaged: PackagedResume | None = None
	session: SessionResult | None = None
	form_intelligence: FormIntelligenceResult | None = None
	llm_field_completion: LLMFieldCompletionResult | None = None
	form_session: FormSession | None = None
	applied: ApplicationResult | None = None
