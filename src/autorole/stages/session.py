from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import httpx

from autorole.config import AppConfig
from autorole.context import JobApplicationContext, SessionResult
from autorole.integrations.credentials import CredentialStore
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


class SessionStage(Stage):
	name = "session"
	concurrency = 1

	PUBLIC_PLATFORMS = {"ashby", "greenhouse", "lever", "workday"}

	def __init__(self, config: AppConfig, credentials: CredentialStore) -> None:
		self._config = config
		self._credentials = credentials

	async def execute(self, message: Message) -> StageResult:
		_ = self._config
		ctx = JobApplicationContext.model_validate(message.payload)
		if ctx.listing is None:
			return StageResult.fail("SessionStage: ctx.listing is None", "PreconditionError")

		platform = ctx.listing.platform.lower()
		if platform in self.PUBLIC_PLATFORMS:
			session = SessionResult(
				platform=platform,
				authenticated=False,
				session_note="public platform - no authentication required",
				established_at=datetime.now(timezone.utc),
			)
			return StageResult.ok(ctx.model_copy(update={"session": session}))

		cookie_key = f"{platform}_cookie"
		cookie_value = self._credentials.get(cookie_key)
		if not cookie_value:
			return StageResult.fail(
				(
					f"No credentials found for platform '{platform}'. "
					f"Run: ar credentials set {cookie_key} <value>"
				),
				"MissingCredentials",
			)

		valid = await _validate_session(platform, cookie_value)
		if not valid:
			return StageResult.fail(
				f"Session cookie for '{platform}' is expired. Re-authenticate and update credentials.",
				"ExpiredSession",
			)

		session = SessionResult(
			platform=platform,
			authenticated=True,
			session_note=f"authenticated via stored cookie for {platform}",
			established_at=datetime.now(timezone.utc),
		)
		return StageResult.ok(ctx.model_copy(update={"session": session}))


async def _validate_session(platform: str, cookie: str) -> bool:
	validation_urls = {
		"linkedin": "https://www.linkedin.com/feed/",
		"indeed": "https://www.indeed.com/account/",
	}
	url = validation_urls.get(platform)
	if url is None:
		return True

	# Cookie name differs per site in reality; this is a lightweight validity probe.
	async with httpx.AsyncClient(cookies={"session": cookie}) as client:
		response = await client.get(url, follow_redirects=False)
		return response.status_code == 200


class SessionExecutor(AutoRoleStage):
	name = "session"

	async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
		_ = attempt
		if ctx.session is None:
			return
		self._write_artifact(
			"output.json",
			json.dumps(ctx.session.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
			ctx.run_id,
		)
		await self._repo.upsert_session(ctx.run_id, ctx.session)

	def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
		_ = attempt
		if ctx.session is None:
			return
		print(f"[ok] session -> authenticated={ctx.session.authenticated}")
