from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

RunId = str
FieldId = str
PageIndex = int

FieldType = Literal[
	"text",
	"textarea",
	"select",
	"radio",
	"checkbox",
	"combobox_search",
	"combobox_lazy",
	"date",
	"file",
	"hidden",
	"unknown",
]

FillAction = Literal["fill", "skip", "human_review"]
DetectionMethod = Literal["url", "dom", "iframe", "fallback"]
FillSource = Literal[
	"profile_direct",
	"profile_inferred",
	"generated",
	"prefilled_ok",
	"no_match",
	"human_filled",
]
FieldStatus = Literal[
	"ok",
	"skipped",
	"selector_not_found",
	"fill_error",
	"human_filled",
]


class ExtractedField(BaseModel):
	id: FieldId
	run_id: RunId
	page_index: PageIndex
	page_label: str
	field_type: FieldType
	selector: str
	label: str
	required: bool
	options: list[str] = Field(default_factory=list)
	prefilled_value: str = ""
	aria_role: str = ""
	extraction_source: Literal["dom", "shadow_dom"] = "dom"


class FillInstruction(BaseModel):
	field_id: FieldId
	run_id: RunId
	action: FillAction
	value: str | None = None
	source: FillSource
	page_index: int = 0


class DetectionResult(BaseModel):
	run_id: RunId
	platform_id: str
	apply_url: str
	used_iframe: bool
	detection_method: DetectionMethod


class FieldOutcome(BaseModel):
	field_id: FieldId
	action_taken: FillAction
	value_used: str | None = None
	status: FieldStatus
	error_message: str | None = None
	strategy_used: str | None = None
	failure_bundle_path: str | None = None


class ExecutionResult(BaseModel):
	run_id: RunId
	success: bool
	platform_id: str
	apply_url: str
	submitted_at: str
	confirmation_text: str
	field_outcomes: list[FieldOutcome] = Field(default_factory=list)
	screenshot_pre: str
	screenshot_post: str
	error: str | None = None


class AuditFieldEntry(BaseModel):
	field_id: FieldId
	page_index: PageIndex
	page_label: str
	field_type: FieldType
	label: str
	required: bool
	options: list[str] = Field(default_factory=list)
	prefilled_value: str = ""
	selector: str
	action: FillAction
	value: str | None = None
	source: FillSource
	status: FieldStatus
	error_message: str | None = None


class RunAuditLog(BaseModel):
	run_id: RunId
	started_at: str
	finished_at: str
	job_url: str
	detection: DetectionResult
	fields: list[AuditFieldEntry] = Field(default_factory=list)
	result: ExecutionResult
	extra: dict[str, Any] = Field(default_factory=dict)
