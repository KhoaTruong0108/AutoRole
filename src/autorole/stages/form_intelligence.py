from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from autorole.config import AppConfig
from autorole.context import FormIntelligenceResult, JobApplicationContext
from autorole.integrations.llm import LLMClient

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


MAX_CAPTCHA_ATTEMPTS = 2


class QuestionnaireAnswers(BaseModel):
	answers: list[dict[str, str]] = Field(default_factory=list)
	unanswered_required: list[str] = Field(default_factory=list)


class CaptchaSolver:
	def __init__(self, api_key: str = "", service: str = "2captcha") -> None:
		self._api_key = api_key
		self._service = service

	async def solve(self, page: Any, captcha_type: str) -> bool:
		_ = (page, captcha_type, self._api_key, self._service)
		return False


class FormIntelligenceStage(Stage):
	name = "form_intelligence"
	concurrency = 1

	def __init__(
		self,
		config: AppConfig,
		llm_client: LLMClient,
		page: Any,
		captcha_solver: CaptchaSolver | None = None,
	) -> None:
		self._config = config
		self._llm = llm_client
		self._page = page
		self._captcha_solver = captcha_solver

	async def execute(self, message: Message) -> StageResult:
		ctx = JobApplicationContext.model_validate(message.payload)
		if ctx.listing is None or ctx.packaged is None:
			return StageResult.fail(
				"FormIntelligenceStage: ctx.listing and ctx.packaged must be set",
				"PreconditionError",
			)

		await _apply_session_cookies(self._page, ctx)
		try:
			await self._page.goto(ctx.listing.job_url, wait_until="networkidle", timeout=30_000)
		except Exception as exc:
			return StageResult.fail(f"Navigation failed: {exc}", "NavigationError")

		for attempt in range(MAX_CAPTCHA_ATTEMPTS + 1):
			captcha = await _detect_captcha(self._page)
			if not captcha:
				break
			if self._captcha_solver is None or attempt == MAX_CAPTCHA_ATTEMPTS:
				return StageResult.fail(
					(
						f"CAPTCHA detected at {ctx.listing.job_url} and could not be solved after "
						f"{attempt} attempt(s). Human intervention required."
					),
					"CaptchaChallenge",
				)
			solved = await self._captcha_solver.solve(self._page, captcha)
			if not solved:
				continue

		try:
			form_json_raw = await _extract_form_fields(self._page)
		except Exception as exc:
			return StageResult.fail(f"Form extraction failed: {exc}", "FormExtractionError")

		questionnaire = _build_questionnaire(form_json_raw)
		user_profile = _load_user_profile(self._config)

		try:
			answers = await self._llm.call(
				system=_build_answering_system_prompt(user_profile),
				user=_render_questionnaire(questionnaire),
				response_model=QuestionnaireAnswers,
			)
		except Exception as exc:
			return StageResult.fail(f"Questionnaire answering failed: {exc}", "LLMResponseError")

		if answers.unanswered_required:
			fields = ", ".join(answers.unanswered_required)
			return StageResult.fail(
				f"AI could not answer required field(s): {fields}",
				"UnansweredRequiredField",
			)

		form_json_filled = _merge_answers(form_json_raw, answers.answers)
		result = FormIntelligenceResult(
			questionnaire=questionnaire,
			form_json_filled=form_json_filled,
			generated_at=datetime.now(timezone.utc),
		)
		return StageResult.ok(ctx.model_copy(update={"form_intelligence": result}))


async def _apply_session_cookies(page: Any, ctx: JobApplicationContext) -> None:
	if ctx.session is None or not ctx.session.authenticated:
		return
	# Session records currently do not carry raw cookie secrets; this is a safe no-op hook.
	_ = page


async def _detect_captcha(page: Any) -> str | None:
	content = (await page.content()).lower()
	if "recaptcha" in content:
		return "recaptcha_v2"
	if "hcaptcha" in content:
		return "hcaptcha"
	if "cf-challenge" in content:
		return "cloudflare"
	return None


async def _extract_form_fields(page: Any) -> dict[str, Any]:
	elements = await page.query_selector_all("input, select, textarea")
	fields: list[dict[str, Any]] = []
	for element in elements:
		name = await element.get_attribute("name") or await element.get_attribute("id")
		if not name:
			continue
		tag = await element.evaluate("el => el.tagName.toLowerCase()")
		type_attr = await element.get_attribute("type")
		label = await element.get_attribute("aria-label") or name
		required = (await element.get_attribute("required")) is not None

		field_type = "text"
		options: list[str] = []
		if tag == "select":
			field_type = "single_choice"
			options = await element.evaluate(
				"el => Array.from(el.options).map(o => o.textContent?.trim() ?? '')"
			)
		elif type_attr in {"checkbox"}:
			field_type = "multiple_choice"
		elif type_attr in {"radio"}:
			field_type = "single_choice"
		elif type_attr == "file":
			field_type = "file_upload"

		fields.append(
			{
				"id": name,
				"label": label,
				"type": field_type,
				"required": required,
				"options": options,
				"value": "" if field_type != "multiple_choice" else [],
			}
		)

	return {"fields": fields}


def _build_questionnaire(form_json_raw: dict[str, Any]) -> list[dict[str, Any]]:
	questionnaire: list[dict[str, Any]] = []
	for field in form_json_raw.get("fields", []):
		value_type = "choice" if field.get("type") in {"single_choice", "multiple_choice"} else "value"
		questionnaire.append(
			{
				"map": f"direct:{field.get('id', '')}:{value_type}",
				"question": field.get("label") or field.get("id") or "",
				"options": field.get("options", []),
				"answer": "",
				"required": bool(field.get("required", False)),
			}
		)
	return questionnaire


def _render_questionnaire(questionnaire: list[dict[str, Any]]) -> str:
	parts: list[str] = []
	for idx, item in enumerate(questionnaire, start=1):
		parts.append(f"## Q{idx}")
		parts.append(f"Map: {item.get('map', '')}")
		parts.append(f"Question: {item.get('question', '')}")
		parts.append("Options:")
		options = item.get("options", [])
		if options:
			for option in options:
				parts.append(f"- {option}")
		else:
			parts.append("- (free text)")
		parts.append(f"Answer: {item.get('answer', '')}")
		parts.append("")
	return "\n".join(parts)


def _build_answering_system_prompt(user_profile: dict[str, Any]) -> str:
	return (
		"You are completing a job application questionnaire. "
		"Use the provided user profile and return strict JSON matching: "
		"{answers: [{map: str, answer: str}], unanswered_required: [str]}.\n\n"
		f"User profile:\n{json.dumps(user_profile, ensure_ascii=True, indent=2)}"
	)


def _load_user_profile(config: AppConfig) -> dict[str, Any]:
	profile_path = Path(config.base_dir).expanduser() / "user_profile.json"
	if not profile_path.exists():
		return {}
	try:
		return json.loads(profile_path.read_text(encoding="utf-8"))
	except Exception:
		return {}


def _merge_answers(
	form_json_raw: dict[str, Any],
	answers: list[dict[str, str]],
) -> dict[str, Any]:
	answers_by_map = {item.get("map", ""): item.get("answer", "") for item in answers}
	merged = dict(form_json_raw)
	fields = [dict(field) for field in merged.get("fields", [])]
	for field in fields:
		value_type = "choice" if field.get("type") in {"single_choice", "multiple_choice"} else "value"
		map_key = f"direct:{field.get('id', '')}:{value_type}"
		if map_key not in answers_by_map:
			continue
		answer = answers_by_map[map_key]
		if field.get("type") == "multiple_choice":
			field["value"] = [part.strip() for part in answer.split(",") if part.strip()] if answer else []
		else:
			field["value"] = answer
	merged["fields"] = fields
	return merged
