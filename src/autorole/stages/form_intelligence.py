from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from autorole.config import AppConfig
from autorole.context import FormIntelligenceResult, FormSession, JobApplicationContext
from autorole.integrations.form_controls.adapters import get_adapter
from autorole.integrations.form_controls.adapters.base import PageSection
from autorole.integrations.form_controls.detector import detect
from autorole.integrations.form_controls.extractor import SemanticFieldExtractor
from autorole.integrations.form_controls.models import ExtractedField
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
		llm_client: Any,
		page: Any,
		captcha_solver: CaptchaSolver | None = None,
		form_extractor: Any | None = None,
	) -> None:
		self._config = config
		_ = llm_client
		self._page = page
		self._captcha_solver = captcha_solver
		self._extractor = form_extractor or SemanticFieldExtractor(page)

	async def execute(self, message: Message) -> StageResult:
		ctx = JobApplicationContext.model_validate(message.payload)
		if ctx.listing is None or ctx.packaged is None:
			return StageResult.fail(
				"FormIntelligenceStage: ctx.listing and ctx.packaged must be set",
				"PreconditionError",
			)

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

		platform_id = form_session.detection.platform_id
		try:
			fields = await self._extractor.extract(page_section, message.run_id, page_index, platform_id)
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
					platform_id,
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

		fi = FormIntelligenceResult(
			page_index=page_index,
			page_label=page_section.label,
			extracted_fields=fields,
			fill_instructions=[],
			generated_at=datetime.now(timezone.utc),
		)

		form_session.all_fields.extend(fields)
		return StageResult.ok(
			ctx.model_copy(update={"form_intelligence": fi, "form_session": form_session})
		)


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
			json.dumps([], indent=2) + "\n",
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
