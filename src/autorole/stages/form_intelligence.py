from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from autorole.config import AppConfig
from autorole.context import FormIntelligenceResult, FormSession, JobApplicationContext
from autorole.integrations.form_controls.adapters import get_adapter
from autorole.integrations.form_controls.adapters.base import PageSection
from autorole.integrations.form_controls.detector import detect
from autorole.integrations.form_controls.exceptions import MappingError
from autorole.integrations.form_controls.extractor import SemanticFieldExtractor
from autorole.integrations.form_controls.mapper import AIFieldMapper
from autorole.integrations.form_controls.models import ExtractedField, FillInstruction
from autorole.integrations.form_controls.profile import load_profile
from autorole.integrations.llm import LLMClient
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
		form_extractor: Any | None = None,
		field_mapper: Any | None = None,
		use_random_questionnaire_answers: bool = False,
	) -> None:
		self._config = config
		self._llm = llm_client
		self._page = page
		self._captcha_solver = captcha_solver
		self._extractor = form_extractor or SemanticFieldExtractor(page)
		self._mapper = field_mapper or AIFieldMapper(llm_client)
		self._use_random_questionnaire_answers = use_random_questionnaire_answers

	async def execute(self, message: Message) -> StageResult:
		ctx = JobApplicationContext.model_validate(message.payload)
		if ctx.listing is None or ctx.packaged is None:
			return StageResult.fail(
				"FormIntelligenceStage: ctx.listing and ctx.packaged must be set",
				"PreconditionError",
			)

		profile_path = Path(self._config.base_dir).expanduser() / "user_profile.json"
		if not profile_path.exists():
			return StageResult.fail("user_profile.json not found", "ConfigError")

		try:
			profile = load_profile(profile_path)
		except Exception as exc:
			return StageResult.fail(f"Failed to load user profile: {exc}", "ConfigError")

		apply_url = ctx.listing.apply_url or ctx.listing.job_url

		form_session = ctx.form_session
		if form_session is None:
			try:
				await self._page.goto(apply_url, wait_until="domcontentloaded", timeout=60_000)
			except Exception as exc:
				return StageResult.fail(f"Navigation failed: {exc}", "NavigationError")

			for attempt in range(MAX_CAPTCHA_ATTEMPTS + 1):
				captcha = None #await _detect_captcha(self._page)
				# print(f"[debug] CAPTCHA detection attempt {attempt}: {captcha}")
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

			detection = await detect(self._page, apply_url, message.run_id)
			adapter = get_adapter(detection.platform_id)
			frame = _find_frame(self._page) if detection.used_iframe else None
			await adapter.setup(self._page, frame)
			form_session = FormSession(detection=detection, page_index=0)
		elif await _needs_navigation_rehydrate(self._page):
			# Resume run starts a fresh browser context; re-open the form page if needed.
			try:
				await self._page.goto(apply_url, wait_until="domcontentloaded", timeout=60_000)
			except Exception as exc:
				return StageResult.fail(f"Rehydrate navigation failed: {exc}", "NavigationError")
			adapter = get_adapter(form_session.detection.platform_id)
			frame = _find_frame(self._page) if form_session.detection.used_iframe else None
			await adapter.setup(self._page, frame)

		page_index = form_session.page_index
		adapter = get_adapter(form_session.detection.platform_id)
		page_section = await adapter.get_current_page_section(self._page)

		try:
			fields = await self._extractor.extract(page_section, message.run_id, page_index)
		except Exception as exc:
			return StageResult.fail(f"Field extraction failed: {exc}", "ExtractionError")
		if len(fields) == 0:
			if hasattr(self._page, "wait_for_timeout"):
				await self._page.wait_for_timeout(1000)
			try:
				fields = await self._extractor.extract(
					PageSection(label=page_section.label, root="body"),
					message.run_id,
					page_index,
				)
			except Exception as exc:
				return StageResult.fail(f"Field extraction retry failed: {exc}", "ExtractionError")

		if len(fields) == 0:
			current_url = (getattr(self._page, "url", "") or "").strip()
			html_sample = ""
			if hasattr(self._page, "content"):
				try:
					html_sample = (await self._page.content())[:220].replace("\n", " ")
				except Exception:
					html_sample = ""
			return StageResult.fail(
				(
					f"No fields extracted on page {page_index} at {apply_url}; "
					f"current_url={current_url}; html_sample={html_sample}"
				),
				"ExtractionError",
			)

		try:
			instructions = await self._map_fields(fields, profile, message.run_id, page_index)
		except MappingError as exc:
			return StageResult.fail(str(exc), "MappingError")
		except Exception as exc:
			return StageResult.fail(f"Mapping failed: {exc}", "MappingError")

		fi = FormIntelligenceResult(
			page_index=page_index,
			page_label=page_section.label,
			extracted_fields=fields,
			fill_instructions=instructions,
			generated_at=datetime.now(timezone.utc),
		)

		form_session.all_fields.extend(fields)
		form_session.all_instructions.extend(instructions)
		return StageResult.ok(
			ctx.model_copy(update={"form_intelligence": fi, "form_session": form_session})
		)

	async def _map_fields(
		self,
		fields: list[ExtractedField],
		profile: Any,
		run_id: str,
		page_index: int,
	) -> list[FillInstruction]:
		if self._use_random_questionnaire_answers:
			return _build_random_instructions(fields, run_id, page_index)
		return await self._mapper.map(fields, profile, run_id, page_index)


async def _detect_captcha(page: Any) -> str | None:
	content = (await page.content()).lower() if hasattr(page, "content") else ""
	if "recaptcha" in content:
		return "recaptcha_v2"
	if "hcaptcha" in content:
		return "hcaptcha"
	if "cf-challenge" in content:
		return "cloudflare"
	return None


def _find_frame(page: Any) -> Any | None:
	for frame in getattr(page, "frames", []):
		if getattr(frame, "url", ""):
			return frame
	return None


async def _needs_navigation_rehydrate(page: Any) -> bool:
	url = (getattr(page, "url", "") or "").strip().lower()
	if not url or url == "about:blank":
		return True
	if not hasattr(page, "content"):
		return False
	try:
		html = (await page.content()).strip().lower()
	except Exception:
		return False
	if not html:
		return True
	return html in {"<html><head></head><body></body></html>", "<html><body></body></html>"}


def _build_random_instructions(
	fields: list[ExtractedField],
	run_id: str,
	page_index: int,
) -> list[FillInstruction]:
	instructions: list[FillInstruction] = []
	for field in fields:
		value: str | None
		action = "fill"
		source = "generated"
		if field.field_type in {"select", "radio", "combobox_lazy"}:
			if field.options:
				value = field.options[0]
				source = "profile_inferred"
			elif field.required:
				value = "N/A"
			else:
				action = "skip"
				value = None
				source = "no_match"
		elif field.field_type == "checkbox":
			value = ",".join(field.options[:1]) if field.options else ""
		elif field.field_type in {"hidden", "file"}:
			action = "skip"
			value = None
			source = "no_match"
		else:
			value = "Test Value"
		instructions.append(
			FillInstruction(
				field_id=field.id,
				run_id=run_id,
				action=action,
				value=value,
				source=source,
				page_index=page_index,
			)
		)
	return instructions


class FormIntelligenceExecutor(AutoRoleStage):
	name = "form_intelligence"

	async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
		_ = attempt
		fi = ctx.form_intelligence
		if fi is None:
			return
		page_label = fi.page_label.replace(" ", "_")[:30] if fi.page_label else "page"
		self._write_artifact(
			f"page_{fi.page_index}_{page_label}_fields.json",
			json.dumps([item.model_dump(mode="json") for item in fi.extracted_fields], indent=2) + "\n",
			ctx.run_id,
		)
		self._write_artifact(
			f"page_{fi.page_index}_{page_label}_instructions.json",
			json.dumps([item.model_dump(mode="json") for item in fi.fill_instructions], indent=2) + "\n",
			ctx.run_id,
		)

	async def on_failure(self, ctx: JobApplicationContext, result: Any, attempt: int) -> JobApplicationContext | None:
		return await super().on_failure(ctx, result, attempt)

	def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
		_ = attempt
		fi = ctx.form_intelligence
		if fi is None:
			return
		print(
			f"[ok] form_intelligence -> page={fi.page_index} "
			f"fields={len(fi.extracted_fields)} label={fi.page_label!r}"
		)
