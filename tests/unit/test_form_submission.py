from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from autorole.context import (
	FormIntelligenceResult,
	FormSession,
	JobApplicationContext,
	LLMFieldCompletionResult,
	PackagedResume,
)
from autorole.integrations.form_controls.models import (
	DetectionResult,
	ExtractedField,
	FieldOutcome,
	FillInstruction,
)
from autorole.stages.form_submission import FormSubmissionStage
from tests.conftest import SAMPLE_LISTING

try:
	from pipeline.types import Message
except Exception:
	class Message:  # pragma: no cover
		def __init__(self, run_id: str, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
			self.run_id = run_id
			self.payload = payload
			self.metadata = metadata or {}


class MockLocator:
	def __init__(self, visible: bool = True) -> None:
		self._visible = visible
		self.files: list[str] = []

	@property
	def first(self) -> "MockLocator":
		return self

	async def count(self) -> int:
		return 1 if self._visible else 0

	async def is_visible(self) -> bool:
		return self._visible

	async def set_input_files(self, value: str) -> None:
		self.files.append(value)

	async def all_text_contents(self) -> list[str]:
		return []

	async def inner_text(self) -> str:
		return "Application submitted"


class MockPage:
	def __init__(self, submit_visible: bool = True) -> None:
		self.submit_visible = submit_visible
		self.click_calls: list[str] = []
		self.screenshots: list[str] = []
		self.file_locator = MockLocator(visible=True)

	def locator(self, selector: str) -> MockLocator:
		if "file" in selector:
			return self.file_locator
		if "submit" in selector:
			return MockLocator(visible=self.submit_visible)
		return MockLocator(visible=True)

	async def click(self, selector: str) -> None:
		self.click_calls.append(selector)

	async def wait_for_load_state(self, _state: str) -> None:
		return None

	async def wait_for_timeout(self, _ms: int) -> None:
		return None

	async def screenshot(self, path: str) -> None:
		self.screenshots.append(path)

	async def content(self) -> str:
		return "thank you"


class StubExecutor:
	def __init__(self, outcomes: list[FieldOutcome]) -> None:
		self.outcomes = outcomes
		self.calls = 0

	async def execute_page(
		self,
		_page: Any,
		_fields: Any,
		_instructions: Any,
		run_id: str = "",
	) -> list[FieldOutcome]:
		_ = run_id
		self.calls += 1
		return self.outcomes


def _ctx() -> JobApplicationContext:
	field = ExtractedField(
		id="field-1",
		run_id="acme_123",
		page_index=0,
		page_label="Application form",
		field_type="text",
		selector="[name='email']",
		label="Email",
		required=True,
		options=[],
		prefilled_value="",
	)
	inst = FillInstruction(
		field_id="field-1",
		run_id="acme_123",
		action="fill",
		value="me@example.com",
		source="generated",
		page_index=0,
	)
	return JobApplicationContext(
		run_id="acme_123",
		listing=SAMPLE_LISTING,
		packaged=PackagedResume(
			resume_id="res-1",
			pdf_path="/tmp/resume.pdf",
			packaged_at=datetime.now(timezone.utc),
		),
		form_session=FormSession(
			detection=DetectionResult(
				run_id="acme_123",
				platform_id="generic",
				apply_url="https://example.com/apply",
				used_iframe=False,
				detection_method="fallback",
			),
			page_index=0,
		),
		form_intelligence=FormIntelligenceResult(
			page_index=0,
			page_label="Application form",
			extracted_fields=[field],
			fill_instructions=[inst],
			generated_at=datetime.now(timezone.utc),
		),
		llm_field_completion=LLMFieldCompletionResult(
			page_index=0,
			page_label="Application form",
			fill_instructions=[inst],
			generated_at=datetime.now(timezone.utc),
		),
	)


async def test_form_submission_increments_page_index(test_config: Any) -> None:
	page = MockPage(submit_visible=False)
	executor = StubExecutor(
		[
			FieldOutcome(
				field_id="field-1",
				action_taken="fill",
				value_used="me@example.com",
				status="ok",
				error_message=None,
			)
		]
	)
	stage = FormSubmissionStage(test_config, page, executor=executor)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.form_session is not None
	assert out_ctx.form_session.page_index == 1
	assert out_ctx.applied is not None


async def test_form_submission_sets_applied_only_on_submit(test_config: Any) -> None:
	page = MockPage(submit_visible=False)
	executor = StubExecutor([])
	stage = FormSubmissionStage(test_config, page, executor=executor)
	ctx = _ctx().model_copy(
		update={
			"form_session": _ctx().form_session.model_copy(
				update={
					"detection": _ctx().form_session.detection.model_copy(update={"platform_id": "workday"})
				}
			)
		}
	)

	result = await stage.execute(Message(run_id="acme_123", payload=ctx.model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.form_session is not None
	assert out_ctx.form_session.last_advance_action == "next_page"
	assert out_ctx.applied is None


async def test_form_submission_fails_without_preconditions(test_config: Any) -> None:
	page = MockPage()
	stage = FormSubmissionStage(test_config, page, executor=StubExecutor([]))
	ctx = JobApplicationContext(run_id="acme_123")

	result = await stage.execute(Message(run_id="acme_123", payload=ctx.model_dump()))

	assert not result.success
	assert result.error_type == "PreconditionError"


async def test_form_submission_dryrun_forces_next_page_action(test_config: Any) -> None:
	page = MockPage()
	stage = FormSubmissionStage(test_config, page, executor=StubExecutor([]))

	result = await stage.execute(
		Message(
			run_id="acme_123",
			payload=_ctx().model_dump(),
			metadata={"dryrun_stop_after_submit": True},
		)
	)

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.form_session is not None
	assert out_ctx.form_session.last_advance_action == "done"
	assert out_ctx.form_session.page_index == 0
	assert page.file_locator.files == ["/tmp/resume.pdf"]


async def test_form_submission_apply_dryrun_mode_skips_submit(test_config: Any) -> None:
	page = MockPage()
	stage = FormSubmissionStage(test_config, page, executor=StubExecutor([]))

	result = await stage.execute(
		Message(
			run_id="acme_123",
			payload=_ctx().model_dump(),
			metadata={"run_mode": "apply-dryrun"},
		)
	)

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.form_session is not None
	assert out_ctx.form_session.last_advance_action == "done"
	assert out_ctx.form_session.page_index == 0


async def test_form_submission_fails_when_llm_completion_missing(test_config: Any) -> None:
	page = MockPage()
	stage = FormSubmissionStage(test_config, page, executor=StubExecutor([]))

	ctx_without_completion = _ctx().model_copy(update={"llm_field_completion": None})
	result = await stage.execute(Message(run_id="acme_123", payload=ctx_without_completion.model_dump()))

	assert not result.success
	assert result.error_type == "PreconditionError"
