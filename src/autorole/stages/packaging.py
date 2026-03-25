from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autorole.config import AppConfig
from autorole.context import JobApplicationContext, PackagedResume
from autorole.integrations.renderer import ResumeRenderer
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


class PackagingStage(Stage):
	name = "packaging"
	concurrency = 3

	def __init__(self, config: AppConfig, renderer: ResumeRenderer) -> None:
		self._config = config
		self._renderer = renderer

	async def execute(self, message: Message) -> StageResult:
		_ = self._config
		ctx = JobApplicationContext.model_validate(message.payload)

		if ctx.tailored is None:
			return StageResult.fail("PackagingStage: ctx.tailored is None", "PreconditionError")

		md_path = Path(ctx.tailored.file_path)
		pdf_path = md_path.with_suffix(".pdf")
		try:
			await self._renderer.render(md_path, pdf_path)
		except Exception as exc:
			return StageResult.fail(f"PDF rendering failed: {exc}", "RenderError")

		packaged = PackagedResume(
			resume_id=ctx.tailored.resume_id,
			pdf_path=str(pdf_path),
			packaged_at=datetime.now(timezone.utc),
		)
		return StageResult.ok(ctx.model_copy(update={"packaged": packaged}))


class PackagingExecutor(AutoRoleStage):
	name = "packaging"

	async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
		_ = attempt
		if ctx.packaged is None:
			return
		self._write_artifact(
			"output.json",
			json.dumps(ctx.packaged.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
			ctx.run_id,
		)

	async def on_failure(self, ctx: JobApplicationContext, result: Any, attempt: int) -> JobApplicationContext | None:
		_ = attempt
		if self._mode == "apply-dryrun" and ctx.tailored is not None:
			fallback = PackagedResume(
				resume_id=ctx.tailored.resume_id,
				pdf_path=ctx.tailored.file_path,
				packaged_at=datetime.now(timezone.utc),
			)
			self._write_artifact(
				"error.txt",
				(
					f"error_type={getattr(result, 'error_type', '')}\n"
					f"error={result.error}\n"
					"fallback=tailored_markdown\n"
				),
				ctx.run_id,
			)
			print(
				f"[warn] packaging failed in apply-dryrun mode; {result.error} "
				"falling back to tailored markdown for upload"
			)
			return ctx.model_copy(update={"packaged": fallback})
		return await super().on_failure(ctx, result, attempt)

	def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
		_ = attempt
		if ctx.packaged is None:
			return
		print(f"[ok] packaging -> pdf={ctx.packaged.pdf_path}")
