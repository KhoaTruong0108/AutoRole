from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from pathlib import Path

from autorole.integrations.form_controls.exceptions import FillError
from autorole.integrations.form_controls.models import (
	AuditFieldEntry,
	DetectionResult,
	ExecutionResult,
	ExtractedField,
	FieldOutcome,
	FillInstruction,
	RunAuditLog,
)


class FormExecutor:
	async def execute_page(
		self,
		page: object,
		fields: list[ExtractedField],
		instructions: list[FillInstruction],
	) -> list[FieldOutcome]:
		instr_map = {inst.field_id: inst for inst in instructions}
		outcomes: list[FieldOutcome] = []

		for field in fields:
			inst = instr_map.get(field.id)
			if inst is None or inst.action != "fill" or not inst.value:
				outcomes.append(
					FieldOutcome(
						field_id=field.id,
						action_taken=inst.action if inst else "skip",
						value_used=None,
						status="skipped",
						error_message=None,
					)
				)
				continue

			try:
				await _fill_field(page, field, inst.value)
				outcomes.append(
					FieldOutcome(
						field_id=field.id,
						action_taken="fill",
						value_used=inst.value,
						status="ok",
						error_message=None,
					)
				)
			except Exception as exc:
				outcomes.append(
					FieldOutcome(
						field_id=field.id,
						action_taken="fill",
						value_used=inst.value,
						status="fill_error",
						error_message=str(exc),
					)
				)

			if hasattr(page, "wait_for_timeout"):
				await page.wait_for_timeout(random.randint(80, 250))

		return outcomes


async def _fill_field(page: object, field: ExtractedField, value: str) -> None:
	if not hasattr(page, "locator"):
		raise FillError("Page object does not support locator API")

	loc = page.locator(field.selector).first
	await loc.wait_for(state="visible", timeout=5_000)

	match field.field_type:
		case "text" | "date":
			await loc.fill("")
			await loc.type(value, delay=30)
		case "textarea":
			await loc.fill("")
			await loc.fill(value)
			await loc.dispatch_event("input")
			await loc.dispatch_event("change")
		case "select":
			try:
				await loc.select_option(label=value)
			except Exception:
				fallback = _pick_top_option(value, field.options)
				if fallback is None:
					raise
				await loc.select_option(label=fallback)
		case "radio":
			target_value = value
			if field.options and value not in field.options:
				target_value = _pick_top_option(value, field.options) or value
			by_label = page.locator(f'label:has-text("{target_value}") input[type="radio"]').first
			if await by_label.count() > 0:
				await by_label.check()
			else:
				await page.locator(f'input[type="radio"][value="{target_value}"]').first.check()
		case "checkbox":
			for option in (part.strip() for part in value.split(",") if part.strip()):
				cb = page.locator(f'label:has-text("{option}") input[type="checkbox"]').first
				if await cb.count() > 0:
					await cb.check()
		case "combobox_search":
			await loc.fill("")
			await loc.type(value, delay=40)
			listbox = page.locator('[role="option"]').first
			try:
				await listbox.wait_for(state="visible", timeout=3_000)
				await listbox.click()
			except Exception:
				return
		case "combobox_lazy":
			await loc.click()
			# Many ATS comboboxes allow typing free text to filter top options.
			try:
				await loc.fill("")
				await loc.type(value, delay=40)
			except Exception:
				pass
			await page.wait_for_selector('[role="option"], [role="menuitem"]', timeout=3_000)
			target = page.locator(
				f'[role="option"]:text-is("{value}"), [role="menuitem"]:text-is("{value}")'
			).first
			if await target.count() == 0:
				partial = page.locator(f'[role="option"]:has-text("{value}"), [role="menuitem"]:has-text("{value}")').first
				if await partial.count() > 0:
					await partial.click()
					return
				top_option = page.locator('[role="option"], [role="menuitem"]').first
				if await top_option.count() > 0:
					await top_option.click()
					return
				raise FillError(
					f'combobox_lazy: option "{value}" not found for "{field.label}"'
				)
			else:
				await target.click()
		case "file" | "hidden":
			return
		case _:
			raise FillError(f"Unsupported field type: {field.field_type}")


def _pick_top_option(suggestion: str, options: list[str]) -> str | None:
	if not options:
		return None
	needle = suggestion.strip().lower()
	if not needle:
		return options[0]

	for option in options:
		opt = option.lower()
		if needle == opt:
			return option

	for option in options:
		opt = option.lower()
		if needle in opt or opt in needle:
			return option

	return options[0]


def _build_audit_log(
	run_id: str,
	started_at: str,
	job_url: str,
	detection: DetectionResult,
	all_fields: list[ExtractedField],
	all_instructions: list[FillInstruction],
	all_outcomes: list[FieldOutcome],
	result: ExecutionResult,
) -> RunAuditLog:
	instr_map = {i.field_id: i for i in all_instructions}
	outcome_map = {o.field_id: o for o in all_outcomes}

	entries: list[AuditFieldEntry] = []
	for field in all_fields:
		inst = instr_map.get(field.id)
		outcome = outcome_map.get(field.id)
		entries.append(
			AuditFieldEntry(
				field_id=field.id,
				page_index=field.page_index,
				page_label=field.page_label,
				field_type=field.field_type,
				label=field.label,
				required=field.required,
				options=field.options,
				prefilled_value=field.prefilled_value,
				selector=field.selector,
				action=(inst.action if inst else "skip"),
				value=(inst.value if inst else None),
				source=(inst.source if inst else "no_match"),
				status=(outcome.status if outcome else "skipped"),
				error_message=(outcome.error_message if outcome else None),
			)
		)

	return RunAuditLog(
		run_id=run_id,
		started_at=started_at,
		finished_at=datetime.now(timezone.utc).isoformat(),
		job_url=job_url,
		detection=detection,
		fields=entries,
		result=result,
	)


def _write_audit_log(log: RunAuditLog, run_id: str) -> str:
	path = Path("logs") / f"{run_id}.json"
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(json.dumps(log.model_dump(mode="json"), indent=2, ensure_ascii=True), encoding="utf-8")
	return str(path)

