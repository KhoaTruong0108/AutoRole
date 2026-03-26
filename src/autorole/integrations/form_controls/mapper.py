from __future__ import annotations

import json
import os
from typing import Any

from autorole.integrations.form_controls.exceptions import MappingError
from autorole.integrations.form_controls.models import ExtractedField, FillInstruction, FillSource
from autorole.integrations.form_controls.profile import UserProfile
from autorole.integrations.llm import LLMClient

MAPPER_SYSTEM_PROMPT = (
	"You map job application fields to a user profile. Return only a JSON array where each item "
	"has: id (field id), action (fill|skip|human_review), and value (string or null). "
	"Never skip required fields. "
	"Only skip a field if there is no relevant information or preference in the user profile at all; otherwise, provide a best-guess value."
	"For option-like fields (select, radio, combobox), when no exact option exists, provide the best free-text suggestion in value instead of skipping."
	"Do not include extra text."
)


def _strip_json_fences(text: str) -> str:
	clean = text.strip()
	if clean.startswith("```"):
		clean = clean.strip("`")
		if clean.startswith("json"):
			clean = clean[4:].strip()
	return clean


def _derive_source(item: dict[str, Any], field: ExtractedField, profile: UserProfile) -> FillSource:
	if item.get("action") != "fill" or not item.get("value"):
		return "no_match"

	value = str(item["value"])
	if field.prefilled_value and field.prefilled_value == value:
		return "prefilled_ok"

	profile_text = json.dumps(profile.model_dump(mode="json"), ensure_ascii=True)
	if f'"{value}"' in profile_text:
		return "profile_direct"
	if value in field.options:
		return "profile_inferred"
	return "generated"


def _validate(instructions: list[FillInstruction], fields: list[ExtractedField]) -> None:
	field_map = {f.id: f for f in fields}
	for inst in instructions:
		field = field_map.get(inst.field_id)
		if field is None:
			raise MappingError(f"Unknown field_id in fill plan: {inst.field_id}")

		if field.required and inst.action == "skip":
			raise MappingError(
				f'Required field "{field.label}" ({inst.field_id}) cannot be skipped'
			)

		if field.field_type in ("select", "radio", "combobox_lazy") and inst.action == "fill":
			if field.required and (inst.value is None or not str(inst.value).strip()):
				raise MappingError(
					f'Required option field "{field.label}" ({inst.field_id}) must have a suggestion value'
				)


def _resolve_human_review(instructions: list[FillInstruction], fields: list[ExtractedField]) -> None:
	field_map = {f.id: f for f in fields}
	for inst in instructions:
		if inst.action != "human_review":
			continue
		field = field_map[inst.field_id]
		if not field.required:
			continue
		value = input(f'\n[REQUIRED] Enter value for "{field.label}": ').strip()
		inst.action = "fill"
		inst.value = value
		inst.source = "human_filled"


def _coerce_required_option_items(
	parsed: list[dict[str, Any]],
	fields: list[ExtractedField],
) -> list[dict[str, Any]]:
	field_map = {f.id: f for f in fields}
	coerced: list[dict[str, Any]] = []
	for item in parsed:
		obj = dict(item)
		field_id = str(obj.get("id") or "")
		field = field_map.get(field_id)
		if field is None:
			coerced.append(obj)
			continue

		if field.field_type not in {"select", "radio", "combobox_lazy", "combobox_search"}:
			coerced.append(obj)
			continue

		action = str(obj.get("action") or "skip")
		value = obj.get("value")
		has_value = value is not None and str(value).strip() != ""
		if field.required and (action == "skip" or not has_value):
			obj["action"] = "fill"
			obj["value"] = field.options[0] if field.options else (field.prefilled_value or "N/A")
		coerced.append(obj)

	return coerced


class AIFieldMapper:
	def __init__(self, llm_client: LLMClient, model: str = "gpt-oss:20b") -> None:
		self._llm = llm_client
		self._model = model

	@staticmethod
	def _debug_enabled() -> bool:
		flag = os.getenv("AUTOROLE_DEBUG_FORM_MAPPING", "")
		return flag.lower() in {"1", "true", "yes", "on"}

	def _debug_print(self, run_id: str, page_index: int, label: str, payload: str) -> None:
		if not self._debug_enabled():
			return
		print(f"[debug][mapper][run_id={run_id}][page={page_index}] {label}")
		print(payload)

	async def map(
		self,
		fields: list[ExtractedField],
		profile: UserProfile,
		run_id: str,
		page_index: int,
	) -> list[FillInstruction]:
		mapper_fields = [
			{
				"id": field.id,
				"label": field.label,
				"required": field.required,
				"type": field.field_type,
				"options": field.options,
				"prefilled": field.prefilled_value,
			}
			for field in fields
		]

		user_message = json.dumps(
			{
				"model_hint": self._model,
				"fields": mapper_fields,
				"profile": profile.model_dump(mode="json"),
			},
			ensure_ascii=True,
		)

		self._debug_print(run_id, page_index, "system_prompt", MAPPER_SYSTEM_PROMPT)
		self._debug_print(run_id, page_index, "user_payload", user_message)

		raw_response = await self._llm.call(system=MAPPER_SYSTEM_PROMPT, user=user_message)
		self._debug_print(run_id, page_index, "raw_response", str(raw_response))
		parsed: list[dict[str, Any]]
		try:
			parsed = json.loads(_strip_json_fences(str(raw_response)))
		except Exception:
			retry_prompt = user_message + "\nReturn only raw JSON, no other text."
			self._debug_print(run_id, page_index, "retry_user_payload", retry_prompt)
			retry_raw = await self._llm.call(system=MAPPER_SYSTEM_PROMPT, user=retry_prompt)
			self._debug_print(run_id, page_index, "retry_raw_response", str(retry_raw))
			try:
				parsed = json.loads(_strip_json_fences(str(retry_raw)))
			except Exception as exc:
				raise MappingError(f"Malformed mapper response JSON: {exc}") from exc

		parsed = _coerce_required_option_items(parsed, fields)

		field_map = {f.id: f for f in fields}
		instructions: list[FillInstruction] = []
		for item in parsed:
			field_id = str(item.get("id") or "")
			if not field_id:
				continue
			field = field_map.get(field_id)
			if field is None:
				raise MappingError(f"Unknown field_id in fill plan: {field_id}")

			action = str(item.get("action") or "skip")
			if action not in {"fill", "skip", "human_review"}:
				action = "skip"
			value = item.get("value")
			value_text = None if value is None else str(value)
			instructions.append(
				FillInstruction(
					field_id=field_id,
					run_id=run_id,
					action=action,
					value=value_text,
					source=_derive_source(item, field, profile),
					page_index=page_index,
				)
			)

		_validate(instructions, fields)
		_resolve_human_review(instructions, fields)
		return instructions

