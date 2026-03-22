from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from autorole.context import FormIntelligenceResult, JobApplicationContext, PackagedResume
from autorole.stages.form_submission import FormSubmissionStage
from tests.conftest import SAMPLE_LISTING

try:
	from pipeline.types import Message
except Exception:
	class Message:  # pragma: no cover - fallback when pipeline package is unavailable
		def __init__(self, run_id: str, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
			self.run_id = run_id
			self.payload = payload
			self.metadata = metadata or {}


class MockPage:
	def __init__(self, content_text: str = "") -> None:
		self._content_text = content_text
		self.fill_calls: list[tuple[str, str]] = []
		self.select_calls: list[tuple[str, str]] = []
		self.check_calls: list[str] = []
		self.uncheck_calls: list[str] = []
		self.click_calls: list[str] = []
		self.file_calls: list[tuple[str, str]] = []
		self.raise_on_fill = False

	async def fill(self, selector: str, value: str) -> None:
		if self.raise_on_fill:
			raise TimeoutError("fill timeout")
		self.fill_calls.append((selector, value))

	async def select_option(self, selector: str, value: str) -> None:
		self.select_calls.append((selector, value))

	async def check(self, selector: str) -> None:
		self.check_calls.append(selector)

	async def uncheck(self, selector: str) -> None:
		self.uncheck_calls.append(selector)

	async def click(self, selector: str) -> None:
		self.click_calls.append(selector)

	async def set_input_files(self, selector: str, path: str) -> None:
		self.file_calls.append((selector, path))

	async def content(self) -> str:
		return self._content_text

	async def wait_for_load_state(self, _state: str) -> None:
		return None


def _ctx() -> JobApplicationContext:
	return JobApplicationContext(
		run_id="acme_123",
		listing=SAMPLE_LISTING,
		packaged=PackagedResume(
			resume_id="res-1",
			pdf_path="/tmp/resume.pdf",
			packaged_at=datetime.now(timezone.utc),
		),
		form_intelligence=FormIntelligenceResult(
			questionnaire=[],
			form_json_filled={
				"fields": [
					{"id": "email", "type": "text", "value": "me@example.com"},
					{"id": "country", "type": "single_choice", "value": "US"},
					{"id": "auth", "type": "multiple_choice", "value": ["US Citizen"]},
				]
			},
			generated_at=datetime.now(timezone.utc),
		),
	)


async def test_form_submission_fills_all_fields(test_config: Any) -> None:
	page = MockPage(content_text="application submitted")
	stage = FormSubmissionStage(test_config, page)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert result.success
	assert any("email" in call[0] for call in page.fill_calls)
	assert any("country" in call[0] for call in page.select_calls)
	assert any("auth" in selector for selector in page.check_calls)


async def test_form_submission_attaches_pdf(test_config: Any) -> None:
	page = MockPage(content_text="application submitted")
	stage = FormSubmissionStage(test_config, page)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert result.success
	assert page.file_calls == [("input[type='file']", "/tmp/resume.pdf")]


async def test_form_submission_marks_submitted_when_confirmed(test_config: Any) -> None:
	page = MockPage(content_text="Thank you, your application submitted")
	stage = FormSubmissionStage(test_config, page)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.applied is not None
	assert out_ctx.applied.submission_confirmed is True
	assert out_ctx.applied.submission_status == "submitted"


async def test_form_submission_marks_unconfirmed_without_confirmation_text(test_config: Any) -> None:
	page = MockPage(content_text="Your form has been sent for processing")
	stage = FormSubmissionStage(test_config, page)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.applied is not None
	assert out_ctx.applied.submission_confirmed is False
	assert out_ctx.applied.submission_status == "unconfirmed"


async def test_form_submission_fails_on_playwright_error(test_config: Any) -> None:
	page = MockPage(content_text="application submitted")
	page.raise_on_fill = True
	stage = FormSubmissionStage(test_config, page)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert not result.success
	assert result.error_type == "TimeoutError"


async def test_form_submission_fails_when_preconditions_not_met(test_config: Any) -> None:
	page = MockPage(content_text="application submitted")
	stage = FormSubmissionStage(test_config, page)
	ctx = JobApplicationContext(run_id="acme_123")

	result = await stage.execute(Message(run_id="acme_123", payload=ctx.model_dump()))

	assert not result.success
	assert result.error_type == "PreconditionError"
