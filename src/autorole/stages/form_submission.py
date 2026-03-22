from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from autorole.config import AppConfig
from autorole.context import ApplicationResult, JobApplicationContext

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


class FormSubmissionStage(Stage):
	name = "form_submission"
	concurrency = 1

	def __init__(self, config: AppConfig, page: Any) -> None:
		self._config = config
		self._page = page

	async def execute(self, message: Message) -> StageResult:
		_ = self._config
		ctx = JobApplicationContext.model_validate(message.payload)
		if ctx.form_intelligence is None or ctx.packaged is None:
			return StageResult.fail(
				"FormSubmissionStage: form_intelligence and packaged must be set",
				"PreconditionError",
			)

		try:
			await _fill_form(self._page, ctx.form_intelligence.form_json_filled)
			await _attach_resume(self._page, ctx.packaged.pdf_path)
			await _submit_form(self._page)
			confirmed = await _confirm_submission(self._page)
		except Exception as exc:
			return StageResult.fail(f"Submission failed: {exc}", type(exc).__name__)

		applied = ApplicationResult(
			resume_id=ctx.packaged.resume_id,
			questionnaire=ctx.form_intelligence.questionnaire,
			form_json=ctx.form_intelligence.form_json_filled,
			submission_status="submitted" if confirmed else "unconfirmed",
			submission_confirmed=confirmed,
			applied_at=datetime.now(timezone.utc),
		)
		return StageResult.ok(ctx.model_copy(update={"applied": applied}))


async def _fill_form(page: Any, form_json_filled: dict[str, Any]) -> None:
	for field in form_json_filled.get("fields", []):
		field_id = field.get("id")
		if not field_id:
			continue
		selector = f"[name='{field_id}'], #{field_id}"
		field_type = field.get("type", "text")
		value = field.get("value")

		if field_type in {"text", "textarea", "email", "tel"}:
			await page.fill(selector, "" if value is None else str(value))
		elif field_type == "single_choice":
			await page.select_option(selector, "" if value is None else str(value))
		elif field_type == "multiple_choice":
			if isinstance(value, list):
				for option in value:
					option_selector = f"{selector}[value='{option}']"
					await page.check(option_selector)
		elif field_type == "checkbox":
			if bool(value):
				await page.check(selector)
			else:
				await page.uncheck(selector)
		elif field_type == "radio":
			await page.click(f"{selector}[value='{value}']")


async def _attach_resume(page: Any, pdf_path: str) -> None:
	await page.set_input_files("input[type='file']", pdf_path)


async def _submit_form(page: Any) -> None:
	await page.click("button[type='submit'], input[type='submit']")
	if hasattr(page, "wait_for_load_state"):
		await page.wait_for_load_state("networkidle")


async def _confirm_submission(page: Any) -> bool:
	content = (await page.content()).lower()
	return any(
		signal in content
		for signal in [
			"application submitted",
			"thank you",
			"we received",
		]
	)
