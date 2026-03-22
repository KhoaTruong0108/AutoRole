from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from autorole.config import AppConfig
from autorole.context import JobApplicationContext
from autorole.db.repository import JobRepository

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


class ConcludingStage(Stage):
	name = "concluding"
	concurrency = 1

	def __init__(self, config: AppConfig, repo: JobRepository) -> None:
		self._config = config
		self._repo = repo

	async def execute(self, message: Message) -> StageResult:
		ctx = JobApplicationContext.model_validate(message.payload)

		required = [ctx.listing, ctx.score, ctx.tailored, ctx.packaged, ctx.applied]
		if any(item is None for item in required):
			return StageResult.fail(
				"ConcludingStage: one or more required context fields are None",
				"PreconditionError",
			)

		try:
			await self._repo.upsert_application(
				run_id=ctx.run_id,
				listing=ctx.listing,
				score=ctx.score,
				tailored=ctx.tailored,
				packaged=ctx.packaged,
				applied=ctx.applied,
			)
			if self._config.retention.auto_prune:
				pruneable = await self._repo.get_pruneable_files(self._config.retention.max_age_days)
				for path in pruneable:
					with contextlib.suppress(FileNotFoundError):
						Path(path).unlink()
		except Exception as exc:
			return StageResult.fail(f"Concluding write failed: {exc}", type(exc).__name__)

		return StageResult.ok(ctx)
