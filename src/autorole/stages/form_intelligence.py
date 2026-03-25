from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from autorole.config import AppConfig
from autorole.context import FormIntelligenceResult, JobApplicationContext
from autorole.integrations.form_controls import AsyncDOMFormExtractor, FormExtractor
from autorole.integrations.llm import LLMClient
from autorole.integrations.scrapers import get_scraper
from autorole.integrations.scrapers.models import ApplicationForm
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
		form_extractor: FormExtractor | None = None,
		use_random_questionnaire_answers: bool = False,
	) -> None:
		self._config = config
		self._llm = llm_client
		self._page = page
		self._captcha_solver = captcha_solver
		self._has_custom_form_extractor = form_extractor is not None
		self._form_extractor = form_extractor or AsyncDOMFormExtractor()
		self._use_random_questionnaire_answers = use_random_questionnaire_answers

	async def execute(self, message: Message) -> StageResult:
		ctx = JobApplicationContext.model_validate(message.payload)
		if ctx.listing is None or ctx.packaged is None:
			return StageResult.fail(
				"FormIntelligenceStage: ctx.listing and ctx.packaged must be set",
				"PreconditionError",
			)

		# Prefer ATS-native form extraction when available, then fall back to generic DOM extraction.
		form_json_raw: dict[str, Any] | None = None
		apply_url = ctx.listing.apply_url or ctx.listing.job_url
		try:
			scraper = get_scraper(ctx.listing.job_url, page=self._page)
			application_form = await scraper.fetch_application_form(apply_url)
			form_json_raw = _application_form_to_form_json(application_form)
			if not form_json_raw.get("fields"):
				form_json_raw = None
		except Exception:
			form_json_raw = None

		if form_json_raw is None:
			await _apply_session_cookies(self._page, ctx)
			try:
				await self._page.goto(apply_url, wait_until="domcontentloaded", timeout=60_000)
			except Exception as exc:
				return StageResult.fail(f"Navigation failed: {exc}", "NavigationError")

			for attempt in range(MAX_CAPTCHA_ATTEMPTS + 1):
				captcha = await _detect_captcha(self._page)
				if not captcha:
					break
				if self._captcha_solver is None or attempt == MAX_CAPTCHA_ATTEMPTS:
					return StageResult.fail(
						(
							f"CAPTCHA detected at {apply_url} and could not be solved after "
							f"{attempt} attempt(s). Human intervention required."
						),
						"CaptchaChallenge",
					)
				solved = await self._captcha_solver.solve(self._page, captcha)
				if not solved:
					continue

			try:
				if self._has_custom_form_extractor:
					form_json_raw = await self._form_extractor.extract(self._page)
				else:
					form_json_raw = await _extract_form_fields(self._page)
			except Exception as exc:
				return StageResult.fail(f"Form extraction failed: {exc}", "FormExtractionError")

		questionnaire = _build_questionnaire(form_json_raw)
		user_profile = _load_user_profile(self._config)

		if self._use_random_questionnaire_answers:
			answers = _answer_questionnaire_with_random_filler(questionnaire)
		else:
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


def _application_form_to_form_json(application_form: ApplicationForm) -> dict[str, Any]:
	fields: list[dict[str, Any]] = []
	for field in application_form.fields:
		field_type = field.field_type
		if field_type in {"text", "textarea", "hidden"}:
			normalized = "text"
		elif field_type in {"select", "radio"}:
			normalized = "single_choice"
		elif field_type == "checkbox":
			normalized = "multiple_choice"
		elif field_type == "file":
			normalized = "file_upload"
		else:
			normalized = "text"

		fields.append(
			{
				"id": field.name,
				"label": field.label or field.name,
				"type": normalized,
				"required": field.required,
				"options": list(field.options),
				"value": [] if normalized == "multiple_choice" else "",
			}
		)

	return {"fields": fields}


async def _apply_session_cookies(page: Any, ctx: JobApplicationContext) -> None:
	if ctx.session is None or not ctx.session.authenticated:
		return
	# Session records currently do not carry raw cookie secrets; this is a safe no-op hook.
	_ = page


async def _detect_captcha(page: Any) -> str | None:
	content = (await page.content()).lower()
	# if "recaptcha" in content:
	# 	return "recaptcha_v2"
	# if "hcaptcha" in content:
	# 	return "hcaptcha"
	# if "cf-challenge" in content:
	# 	return "cloudflare"
	return None


async def _extract_form_fields(page: Any) -> dict[str, Any]:
	# Backward-compatible helper retained for tests and legacy call sites.
	return await AsyncDOMFormExtractor().extract(page)


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


def _answer_questionnaire_with_random_filler(questionnaire: list[dict[str, Any]]) -> QuestionnaireAnswers:
	from autorole.mock_data.fill_questionnaire_random import fill_questionnaire_text

	rendered = _render_questionnaire(questionnaire)
	filled = fill_questionnaire_text(rendered)
	return _parse_questionnaire_answers_from_markdown(filled)


def _parse_questionnaire_answers_from_markdown(text: str) -> QuestionnaireAnswers:
	answers: list[dict[str, str]] = []
	unanswered_required: list[str] = []

	question_pattern = re.compile(r"^Question:\s*(.*)$")
	map_pattern = re.compile(r"^Map:\s*(.*)$")
	answer_pattern = re.compile(r"^Answer:\s*(.*)$")

	current_map = ""
	current_question = ""

	for raw_line in text.splitlines():
		line = raw_line.strip()
		if line.startswith("## Q"):
			current_map = ""
			current_question = ""
			continue

		map_match = map_pattern.match(line)
		if map_match:
			current_map = map_match.group(1).strip()
			continue

		question_match = question_pattern.match(line)
		if question_match:
			current_question = question_match.group(1).strip()
			continue

		answer_match = answer_pattern.match(line)
		if not answer_match:
			continue

		answer = answer_match.group(1).strip()
		if not current_map:
			continue
		answers.append({"map": current_map, "answer": answer})

		is_required = "*" in current_question
		if is_required and not answer:
			unanswered_required.append(current_map)

	return QuestionnaireAnswers(answers=answers, unanswered_required=unanswered_required)


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


class FormIntelligenceExecutor(AutoRoleStage):
	name = "form_intelligence"

	async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
		_ = attempt
		fi = ctx.form_intelligence
		if fi is None:
			return
		self._write_artifact(
			"questionnaire.json",
			json.dumps(fi.questionnaire, indent=2, ensure_ascii=False) + "\n",
			ctx.run_id,
		)
		self._write_artifact(
			"form_json_filled.json",
			json.dumps(fi.form_json_filled, indent=2, ensure_ascii=False) + "\n",
			ctx.run_id,
		)
		md_lines = [
			"# Answered Form",
			"",
			"## Questionnaire",
			"",
			json.dumps(fi.questionnaire, indent=2, ensure_ascii=False),
			"",
			"## Filled Form JSON",
			"",
			json.dumps(fi.form_json_filled, indent=2, ensure_ascii=False),
			"",
		]
		self._write_artifact("answered_form.md", "\n".join(md_lines), ctx.run_id)

	async def on_failure(self, ctx: JobApplicationContext, result: Any, attempt: int) -> JobApplicationContext | None:
		_ = attempt
		if self._mode == "apply-dryrun":
			fallback = FormIntelligenceResult(
				questionnaire=[],
				form_json_filled={"fields": []},
				generated_at=datetime.now(timezone.utc),
			)
			self._write_artifact(
				"error.txt",
				(
					f"error_type={getattr(result, 'error_type', '')}\n"
					f"error={result.error}\n"
					"fallback=empty_form_payload\n"
				),
				ctx.run_id,
			)
			self._write_artifact(
				"answered_form.md",
				(
					"# Answered Form\n\n"
					"Form intelligence failed; fallback empty payload was used in apply-dryrun mode.\n\n"
					"## Questionnaire\n\n[]\n\n"
					"## Filled Form JSON\n\n{\n  \"fields\": []\n}\n"
				),
				ctx.run_id,
			)
			print(
				f"[warn] form_intelligence failed in apply-dryrun mode; {result.error} "
				"continuing with empty form payload"
			)
			return ctx.model_copy(update={"form_intelligence": fallback})
		return await super().on_failure(ctx, result, attempt)

	def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
		_ = attempt
		print("[ok] form_intelligence -> form extracted and filled")
