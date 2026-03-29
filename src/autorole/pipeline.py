from __future__ import annotations

import argparse
import asyncio
from typing import Any

from autorole.config import AppConfig
from autorole.context import JobApplicationContext
from autorole.integrations.discovery import build_discovery_providers
from autorole.stages.exploring import ExploringStage

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
		def __init__(self, success: bool, output: Any = None, error: str | None = None) -> None:
			self.success = success
			self.output = output
			self.error = error

		@classmethod
		def ok(cls, output: Any) -> "StageResult":
			return cls(success=True, output=output)

		@classmethod
		def fail(cls, error: str) -> "StageResult":
			return cls(success=False, error=error)


class StubStage(Stage):
	"""Used only during Phase 1 dry-run."""

	def __init__(self, name: str) -> None:
		self.name = name

	async def execute(self, message: Message) -> StageResult:
		ctx = JobApplicationContext.model_validate(message.payload)
		return StageResult.ok(ctx)


class _DryRunPipeline:
	def __init__(self, stages: list[Stage]) -> None:
		self._stages = stages

	async def __aenter__(self) -> "_DryRunPipeline":
		return self

	async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
		return None

	async def run(
		self,
		payload: JobApplicationContext,
		run_id: str,
		metadata: dict[str, Any] | None = None,
	) -> JobApplicationContext:
		message = Message(run_id=run_id, payload=payload, metadata=metadata or {})
		current = payload
		for stage in self._stages:
			result = await stage.execute(message)
			if not result.success:
				raise RuntimeError(result.error or f"Stage {getattr(stage, 'name', '?')} failed")
			current = JobApplicationContext.model_validate(result.output)
			message = Message(run_id=run_id, payload=current, metadata=metadata or {})
		return current


async def build_pipeline(config: AppConfig) -> tuple[_DryRunPipeline, ExploringStage]:
	_ = config
	exploring = ExploringStage(
		config,
		scrapers={},
		discovery_providers=build_discovery_providers(config.search.platforms, llm_client=None),
	)
	pipeline = _DryRunPipeline(
		stages=[
			StubStage("scoring"),
			StubStage("tailoring"),
			StubStage("packaging"),
			StubStage("session"),
			StubStage("form_intelligence"),
			StubStage("form_submission"),
			StubStage("concluding"),
		]
	)
	return pipeline, exploring


async def run_daily(config: AppConfig) -> None:
	pipeline, exploring = await build_pipeline(config)
	seed = Message(run_id="seed", payload={"search_config": config.search.model_dump()})

	async with pipeline:
		result = await exploring.execute(seed)
		if not result.success:
			return

		for ctx in result.output:
			await pipeline.run(payload=ctx, run_id=ctx.run_id, metadata={"source": "daily_cron"})


def _parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="AutoRole pipeline runner")
	parser.add_argument("--dry-run", action="store_true", help="Build and run stub pipeline only")
	return parser.parse_args()


def main() -> None:
	_args = _parse_args()
	asyncio.run(run_daily(AppConfig()))


def inject_loop_metadata_from_gate_reason(
	metadata: dict[str, Any] | None,
	gate_reason: str,
) -> dict[str, Any]:
	"""Inject last_score_before_tailoring from gate LOOP reason if present."""
	base = dict(metadata or {})
	prefix = "first_tailoring|baseline="
	if prefix not in gate_reason:
		return base
	try:
		baseline = float(gate_reason.split(prefix, 1)[1].strip())
	except Exception:
		return base
	base["last_score_before_tailoring"] = baseline
	return base


if __name__ == "__main__":
	main()
