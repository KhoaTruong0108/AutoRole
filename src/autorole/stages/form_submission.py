from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from autorole.config import AppConfig
from autorole.context import ApplicationResult, JobApplicationContext
from autorole.integrations.form_controls import AsyncDOMFormApplier, FormApplier

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

	def __init__(self, config: AppConfig, page: Any, form_applier: FormApplier | None = None) -> None:
		self._config = config
		self._page = page
		self._form_applier = form_applier or AsyncDOMFormApplier()

	async def execute(self, message: Message) -> StageResult:
		_ = self._config
		ctx = JobApplicationContext.model_validate(message.payload)
		metadata = getattr(message, "metadata", {}) or {}
		# Backward-compatible flag: dryrun_stop_after_submit now means skip submit click.
		dryrun_skip_submit = bool(
			metadata.get("dryrun_skip_submit", metadata.get("dryrun_stop_after_submit", False))
		)
		if ctx.listing is None or ctx.form_intelligence is None or ctx.packaged is None:
			return StageResult.fail(
				"FormSubmissionStage: listing, form_intelligence and packaged must be set",
				"PreconditionError",
			)

		if hasattr(self._page, "goto"):
			apply_url = ctx.listing.apply_url or ctx.listing.job_url
			try:
				await self._page.goto(apply_url, wait_until="domcontentloaded", timeout=60_000)
			except Exception as exc:
				return StageResult.fail(f"Navigation failed: {exc}", "NavigationError")

		try:
			await self._form_applier.fill(self._page, ctx.form_intelligence.form_json_filled)
			fill_report = await _build_fill_report(self._page, ctx.form_intelligence.form_json_filled)
			try:
				await self._form_applier.attach_resume(self._page, ctx.packaged.pdf_path)
				fill_report["resume_attachment"] = {
					"expected_path": ctx.packaged.pdf_path,
					"status": "attached",
				}
			except Exception:
				fill_report["resume_attachment"] = {
					"expected_path": ctx.packaged.pdf_path,
					"status": "failed" if not dryrun_skip_submit else "skipped_dryrun",
				}
				if not dryrun_skip_submit:
					raise
			confirmed = False
			submission_status = "dryrun_submit_skipped" if dryrun_skip_submit else "submitted"

			if not dryrun_skip_submit:
				await self._form_applier.submit(self._page)
				confirmed = await self._form_applier.confirm(self._page)
				submission_status = "submitted" if confirmed else "unconfirmed"
		except Exception as exc:
			return StageResult.fail(f"Submission failed: {exc}", type(exc).__name__)

		applied = ApplicationResult(
			resume_id=ctx.packaged.resume_id,
			questionnaire=ctx.form_intelligence.questionnaire,
			form_json=ctx.form_intelligence.form_json_filled,
			fill_report=fill_report,
			submission_status=submission_status,
			submission_confirmed=confirmed,
			applied_at=datetime.now(timezone.utc),
		)
		return StageResult.ok(ctx.model_copy(update={"applied": applied}))


async def _build_fill_report(page: Any, form_json_filled: dict[str, Any]) -> dict[str, Any]:
	fields_report: list[dict[str, Any]] = []
	for field in form_json_filled.get("fields", []):
		field_id = str(field.get("id") or "").strip()
		field_type = str(field.get("type") or "text")
		expected = field.get("value")
		entry = {
			"id": field_id,
			"label": field.get("label") or field_id,
			"type": field_type,
			"expected": expected,
			"actual": None,
			"status": "unknown",
			"error": "",
		}

		if not field_id:
			entry["status"] = "mismatched"
			entry["error"] = "missing_field_id"
			fields_report.append(entry)
			continue

		if not hasattr(page, "locator"):
			entry["error"] = "page_has_no_locator"
			fields_report.append(entry)
			continue

		selector = _name_or_id_selector(field_id)
		try:
			actual = await _read_field_value(page, selector, field_type)
			entry["actual"] = actual
			entry["status"] = "matched" if _values_match(expected, actual) else "mismatched"
		except Exception as exc:
			entry["error"] = str(exc)
		fields_report.append(entry)

	total = len(fields_report)
	matched = sum(1 for item in fields_report if item["status"] == "matched")
	mismatched = sum(1 for item in fields_report if item["status"] == "mismatched")
	unknown = total - matched - mismatched

	return {
		"summary": {
			"total_fields": total,
			"matched": matched,
			"mismatched": mismatched,
			"unknown": unknown,
		},
		"fields": fields_report,
	}


def _name_or_id_selector(field_id: str) -> str:
	return f"[name='{field_id}'], [id='{field_id}']"


async def _read_field_value(page: Any, selector: str, field_type: str) -> Any:
	locator = page.locator(selector)
	count = await locator.count()
	if count == 0:
		raise RuntimeError("field_not_found")

	entries = await locator.evaluate_all(
		"""
		els => els.map(el => {
		  const tag = (el.tagName || '').toLowerCase();
		  const typ = ((el.type || '') + '').toLowerCase();
		  if (tag === 'select') {
		    const selected = Array.from(el.selectedOptions || []).map(o => o.value || o.textContent || '');
		    return { tag, type: 'select', value: selected.length <= 1 ? (selected[0] || '') : selected, checked: null };
		  }
		  if (typ === 'checkbox' || typ === 'radio') {
		    return { tag, type: typ, value: el.value || '', checked: !!el.checked };
		  }
		  return { tag, type: typ || tag, value: el.value || '', checked: null };
		})
		"""
	)

	if field_type == "multiple_choice":
		selected = [str(item.get("value") or "") for item in entries if item.get("checked")]
		return [value for value in selected if value]

	if field_type == "checkbox":
		return any(bool(item.get("checked")) for item in entries)

	if field_type in {"radio", "single_choice"}:
		for item in entries:
			if item.get("checked"):
				return str(item.get("value") or "")
		for item in entries:
			value = item.get("value")
			if isinstance(value, list) and value:
				return str(value[0])
			if isinstance(value, str) and value:
				return value
		return ""

	value = entries[0].get("value")
	if isinstance(value, list):
		return value[0] if value else ""
	return str(value or "")


def _values_match(expected: Any, actual: Any) -> bool:
	if isinstance(expected, list):
		expected_norm = sorted(str(v).strip() for v in expected)
		actual_values = actual if isinstance(actual, list) else ([] if actual is None else [actual])
		actual_norm = sorted(str(v).strip() for v in actual_values)
		return expected_norm == actual_norm
	if isinstance(expected, bool):
		return bool(actual) == expected
	return str(expected or "").strip() == str(actual or "").strip()


async def _fill_form(page: Any, form_json_filled: dict[str, Any]) -> None:
	await AsyncDOMFormApplier().fill(page, form_json_filled)


async def _attach_resume(page: Any, pdf_path: str) -> None:
	await AsyncDOMFormApplier().attach_resume(page, pdf_path)


async def _submit_form(page: Any) -> None:
	await AsyncDOMFormApplier().submit(page)


async def _confirm_submission(page: Any) -> bool:
	return await AsyncDOMFormApplier().confirm(page)
