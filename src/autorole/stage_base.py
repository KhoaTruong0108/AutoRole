from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autorole.config import AppConfig
from autorole.context import JobApplicationContext
from autorole.db.repository import JobRepository

STAGE_ORDER = [
	"exploring",
	"scoring",
	"tailoring",
	"packaging",
	"session",
	"form_intelligence",
	"form_submission",
	"concluding",
]


def _build_resume_command(run_id: str, mode: str, from_stage: str) -> str:
	return (
		"PYTHONPATH=src python3 scripts/run_real_pipeline.py "
		f"--resume-run-id {run_id} --from-stage {from_stage} --mode {mode}"
	)


def _emit_resume_hint(logger: logging.Logger, run_id: str, mode: str, from_stage: str) -> None:
	cmd = _build_resume_command(run_id, mode, from_stage)
	print(f"[resume-cmd] {cmd}")
	logger.info("RESUME_COMMAND run_id=%s stage=%s cmd=%s", run_id, from_stage, cmd)


class AutoRoleStage(ABC):
	"""Template wrapper around pure SnapFlow stages."""

	name: str

	def __init__(
		self,
		stage: Any,
		repo: JobRepository,
		logger: logging.Logger,
		artifacts_root: Path,
		mode: str,
		config: AppConfig,
	) -> None:
		self._stage = stage
		self._repo = repo
		self._logger = logger
		self._artifacts_root = artifacts_root
		self._mode = mode
		self._config = config

	def should_run(self, start_stage: str) -> bool:
		return STAGE_ORDER.index(self.name) >= STAGE_ORDER.index(start_stage)

	async def run(
		self,
		ctx: JobApplicationContext,
		*,
		attempt: int = 1,
		metadata: dict[str, Any] | None = None,
	) -> JobApplicationContext | None:
		msg = self._build_message(ctx, attempt, metadata or {})
		result = await self._execute_inner(msg)

		if result is None:
			self._logger.exception(
				"Unhandled exception in stage=%s run_id=%s", self.name, ctx.run_id
			)
			print(f"[fail] {self.name}: unhandled exception (see trace log)")
			_emit_resume_hint(self._logger, ctx.run_id, self._mode, self.name)
			return None

		if not result.success:
			self._logger.error(
				"Stage %s failed run_id=%s attempt=%s error_type=%s error=%s",
				self.name,
				ctx.run_id,
				attempt,
				getattr(result, "error_type", None),
				result.error,
			)
			return await self.on_failure(ctx, result, attempt)

		new_ctx = JobApplicationContext.model_validate(result.output)
		await self.on_success(new_ctx, attempt)
		await self._repo.upsert_checkpoint(new_ctx.run_id, self.name, new_ctx.model_dump(mode="json"))
		self.log_ok(new_ctx, attempt)
		return new_ctx

	def _build_message(
		self,
		ctx: JobApplicationContext,
		attempt: int,
		metadata: dict[str, Any],
	) -> Any:
		from autorole.pipeline import Message
		try:
			return Message(
				run_id=ctx.run_id,
				payload=ctx.model_dump(),
				metadata=metadata,
				attempt=attempt,
			)
		except TypeError:
			return Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata=metadata)

	async def on_failure(
		self,
		ctx: JobApplicationContext,
		result: Any,
		attempt: int,
	) -> JobApplicationContext | None:
		_ = attempt
		print(f"[fail] {self.name}: {result.error}")
		self._write_artifact(
			"error.txt",
			f"error_type={getattr(result, 'error_type', '')}\nerror={result.error}\n",
			ctx.run_id,
		)
		_emit_resume_hint(self._logger, ctx.run_id, self._mode, self.name)
		return None

	@abstractmethod
	async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
		...

	@abstractmethod
	def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
		...

	def _artifact_dir(self, run_id: str) -> Path:
		return self._artifacts_root / run_id / self.name

	def _write_artifact(self, filename: str, content: str, run_id: str) -> Path:
		run_dir = self._artifacts_root / run_id
		artifact_path = run_dir / self.name / filename
		artifact_path.parent.mkdir(parents=True, exist_ok=True)
		artifact_path.write_text(content, encoding="utf-8")
		self._append_stage_index(run_dir, filename)
		self._logger.info("STAGE_ARTIFACT stage=%s path=%s", self.name, artifact_path)
		return artifact_path

	def _append_stage_index(self, run_dir: Path, filename: str) -> None:
		index_path = run_dir / "stage_outputs.md"
		rel_path = Path(self.name) / filename
		with index_path.open("a", encoding="utf-8") as handle:
			handle.write(f"- {self.name}: {rel_path}\n")

	async def _execute_inner(self, msg: Any) -> Any | None:
		try:
			return await self._stage.execute(msg)
		except Exception:
			return None


__all__ = ["AutoRoleStage", "STAGE_ORDER", "_emit_resume_hint"]
