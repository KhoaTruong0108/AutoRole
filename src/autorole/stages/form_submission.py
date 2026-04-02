from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autorole.config import AppConfig
from autorole.context import ApplicationResult, JobApplicationContext
from autorole.integrations.form_controls.adapters import get_adapter
from autorole.integrations.form_controls.executor import FormExecutor, _build_audit_log, _write_audit_log
from autorole.integrations.form_controls.models import ExecutionResult
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


class FormSubmissionStage(Stage):
	name = "form_submission"
	concurrency = 1

	def __init__(self, config: AppConfig, page: Any, executor: FormExecutor | None = None) -> None:
		self._config = config
		self._page = page
		self._executor = executor or FormExecutor()

	async def execute(self, message: Message) -> StageResult:
		_ = self._config
		ctx = JobApplicationContext.model_validate(message.payload)
		metadata = getattr(message, "metadata", {}) or {}
		run_mode = metadata.get("run_mode") if isinstance(metadata, dict) else None
		dryrun_skip_submit = bool(metadata.get("dryrun_stop_after_submit", False)) or run_mode == "apply-dryrun"

		if (
			ctx.listing is None
			or ctx.form_intelligence is None
			or ctx.llm_field_completion is None
			or ctx.form_session is None
			or ctx.packaged is None
		):
			return StageResult.fail(
				(
					"FormSubmissionStage: listing, form_intelligence, llm_field_completion, "
					"form_session and packaged must be set"
				),
				"PreconditionError",
			)

		fields = ctx.form_intelligence.extracted_fields
		instructions = ctx.llm_field_completion.fill_instructions
		page_index = ctx.form_intelligence.page_index
		platform_id = ctx.form_session.detection.platform_id
		adapter = get_adapter(platform_id)

		outcomes = await self._executor.execute_page(
			self._page,
			fields,
			instructions,
			run_id=ctx.run_id,
		)
		field_map = {field.id: field for field in fields}
		required_failures = [
			outcome
			for outcome in outcomes
			if outcome.status in {"fill_error", "selector_not_found"}
			and field_map.get(outcome.field_id, None) is not None
			and field_map[outcome.field_id].required
		]

		file_input = await adapter.get_file_input(self._page)
		if file_input is not None:
			await file_input.set_input_files(str(ctx.packaged.pdf_path))
			if hasattr(self._page, "wait_for_timeout"):
				await self._page.wait_for_timeout(500)

		artifacts_dir = Path("logs") / ctx.run_id
		artifacts_dir.mkdir(parents=True, exist_ok=True)
		page_section_label = (ctx.form_intelligence.page_label or f"page_{page_index}").replace(" ", "_")[:40]
		screenshot_path = str(artifacts_dir / f"page_{page_index}_{page_section_label}.png")
		if hasattr(self._page, "screenshot"):
			await self._page.screenshot(path=screenshot_path)

		if required_failures:
			failed_ids = ", ".join(outcome.field_id for outcome in required_failures)
			return StageResult.fail(
				f"Required field(s) could not be filled — refusing to advance. failing_field_ids=[{failed_ids}]",
				"RequiredFieldFillError",
			)

		action = "done" if dryrun_skip_submit else await adapter.advance(self._page)
		applied = None

		if action == "submit":
			post_submit = str(artifacts_dir / "post_submit.png")
			if hasattr(self._page, "screenshot"):
				await self._page.screenshot(path=post_submit)
			success = await adapter.confirm_success(self._page)
			if not success:
				errors: list[str] = []
				if hasattr(self._page, "locator"):
					try:
						errors = await self._page.locator('[class*="error"], [role="alert"]').all_text_contents()
					except Exception:
						errors = []
				return StageResult.fail(
					f"Submission not confirmed. Page errors: {errors}",
					"SubmissionError",
				)

			confirmation_text = ""
			if hasattr(self._page, "locator"):
				try:
					confirmation_text = (await self._page.locator("body").inner_text())[:500]
				except Exception:
					confirmation_text = ""

			all_outcomes = ctx.form_session.all_outcomes + outcomes
			execution_result = ExecutionResult(
				run_id=ctx.run_id,
				success=True,
				platform_id=platform_id,
				apply_url=ctx.form_session.detection.apply_url,
				submitted_at=datetime.now(timezone.utc).isoformat(),
				confirmation_text=confirmation_text,
				field_outcomes=all_outcomes,
				screenshot_pre=screenshot_path,
				screenshot_post=post_submit,
				error=None,
			)

			audit = _build_audit_log(
				run_id=ctx.run_id,
				started_at=ctx.started_at.isoformat(),
				job_url=ctx.listing.job_url,
				detection=ctx.form_session.detection,
				all_fields=ctx.form_session.all_fields + fields,
				all_instructions=ctx.form_session.all_instructions + instructions,
				all_outcomes=all_outcomes,
				result=execution_result,
			)
			audit_log_path = _write_audit_log(audit, ctx.run_id)

			applied = ApplicationResult(
				resume_id=ctx.packaged.resume_id,
				execution_result=execution_result,
				audit_log_path=audit_log_path,
				applied_at=datetime.now(timezone.utc),
				submission_status="submitted",
				submission_confirmed=True,
			)

		session = ctx.form_session
		session.all_outcomes.extend(outcomes)
		session.screenshots.append(screenshot_path)
		session.last_advance_action = action
		if action in {"next_page", "submit"}:
			session.page_index += 1

		if action == "submit":
			return StageResult.ok(ctx.model_copy(update={"form_session": session, "applied": applied}))
		return StageResult.ok(ctx.model_copy(update={"form_session": session}))


class FormSubmissionExecutor(AutoRoleStage):
	name = "form_submission"

	def _build_message(self, ctx: JobApplicationContext, attempt: int, metadata: dict[str, Any]) -> Any:
		return super()._build_message(
			ctx,
			attempt,
			{
				**metadata,
				"dryrun_stop_after_submit": self._mode == "apply-dryrun",
			},
		)

	async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
		_ = attempt
		session = ctx.form_session
		if session is None:
			return
		self._write_artifact(
			f"page_{session.page_index - 1}_outcomes.json",
			json.dumps([item.model_dump(mode="json") for item in session.all_outcomes], indent=2) + "\n",
			ctx.run_id,
		)
		if ctx.applied is not None and ctx.applied.execution_result is not None:
			self._write_artifact(
				"execution_result.json",
				json.dumps(ctx.applied.execution_result.model_dump(mode="json"), indent=2) + "\n",
				ctx.run_id,
			)

	def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
		_ = attempt
		session = ctx.form_session
		if session is None:
			return
		action = session.last_advance_action
		page = session.page_index - 1
		print(f"[ok] form_submission -> page={page} advance={action}")
