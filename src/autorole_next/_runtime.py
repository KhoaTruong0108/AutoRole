from ._snapflow import PipelineRunner, PipelineSeeder, RunStatus, SQLiteQueueAdapter, SQLiteStoreAdapter

__all__ = [
    "PipelineRunner",
    "PipelineSeeder",
    "RunStatus",
    "SQLiteQueueAdapter",
    "SQLiteStoreAdapter",
]from __future__ import annotations

import abc
import asyncio
import enum
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite
import orjson
import pydantic


def _json_default(value: Any) -> Any:
	if hasattr(value, "model_dump"):
		return value.model_dump(mode="json")
	if isinstance(value, datetime):
		return value.isoformat()
	raise TypeError(f"Object of type {type(value)!r} is not JSON serializable")


class RunStatus(str, enum.Enum):
	QUEUED = "queued"
	RUNNING = "running"
	COMPLETED = "completed"
	BLOCKED = "blocked"
	ERROR = "error"


class StageResult(pydantic.BaseModel):
	model_config = pydantic.ConfigDict(frozen=True)

	success: bool
	output: Any = None
	error: str | None = None
	error_type: str | None = None
	duration_ms: float = 0.0

	@classmethod
	def ok(cls, output: Any, duration_ms: float = 0.0) -> StageResult:
		return cls(success=True, output=output, duration_ms=duration_ms)

	@classmethod
	def fail(
		cls,
		error: str,
		error_type: str = "",
		duration_ms: float = 0.0,
	) -> StageResult:
		return cls(
			success=False,
			output=None,
			error=error,
			error_type=error_type or "",
			duration_ms=duration_ms,
		)


class StateContext(pydantic.BaseModel):
	model_config = pydantic.ConfigDict(frozen=True)

	correlation_id: str
	current_stage: str
	attempt: int = 1
	data: Any
	metadata: dict[str, Any] = pydantic.Field(default_factory=dict)
	artifact_refs: list[str] = pydantic.Field(default_factory=list)
	created_at: datetime = pydantic.Field(default_factory=lambda: datetime.now(timezone.utc))


class RunRecord(pydantic.BaseModel):
	model_config = pydantic.ConfigDict(frozen=True)

	correlation_id: str
	status: RunStatus
	reason: str = ""
	started_at: datetime
	updated_at: datetime
	stage_count: int = 0


class Executor(abc.ABC):
	concurrency: int = 1

	@abc.abstractmethod
	async def execute(self, context: StateContext) -> StageResult:
		...


class Gate(abc.ABC):
	@abc.abstractmethod
	def route(
		self,
		result: StageResult,
		context: StateContext,
		default_next_stage: str | None,
	) -> str | None:
		...


class PassThroughGate(Gate):
	def route(
		self,
		result: StageResult,
		context: StateContext,
		default_next_stage: str | None,
	) -> str | None:
		_ = (result, context)
		return default_next_stage


@dataclass(frozen=True)
class StageNode:
	id: str
	executor: Executor
	label: str = ""


class QueueAdapter(abc.ABC):
	@abc.abstractmethod
	async def put(self, context: StateContext) -> None:
		...

	@abc.abstractmethod
	async def get(self) -> StateContext:
		...

	@abc.abstractmethod
	async def close(self) -> None:
		...

	@property
	@abc.abstractmethod
	def depth(self) -> int:
		...


class QueueBackend(abc.ABC):
	@abc.abstractmethod
	def create_queue(self, stage_id: str) -> QueueAdapter:
		...


class SQLiteQueueAdapter(QueueAdapter):
	def __init__(self, path: str, stage_id: str) -> None:
		self._path = str(Path(path).expanduser())
		self._stage_id = stage_id

	def _now_iso(self) -> str:
		return datetime.now(timezone.utc).isoformat()

	def _dumps(self, value: Any) -> str:
		return orjson.dumps(value, default=_json_default).decode("utf-8")

	async def put(self, context: StateContext) -> None:
		message_id = str(uuid4())
		now = self._now_iso()
		payload_json = self._dumps(context.model_dump(mode="json"))
		async with aiosqlite.connect(self._path) as db:
			await db.execute(
				"""
				INSERT INTO queue_messages (
					message_id,
					correlation_id,
					stage_name,
					payload_json,
					status,
					visible_after,
					created_at,
					updated_at
				) VALUES (?, ?, ?, ?, 'queued', NULL, ?, ?)
				""",
				(
					message_id,
					context.correlation_id,
					self._stage_id,
					payload_json,
					now,
					now,
				),
			)
			await db.commit()

	async def get(self) -> StateContext:
		while True:
			async with aiosqlite.connect(self._path) as db:
				await db.execute("BEGIN IMMEDIATE")
				async with db.execute(
					"""
					SELECT message_id, payload_json
					FROM queue_messages
					WHERE stage_name = ? AND status = 'queued'
					ORDER BY created_at ASC
					LIMIT 1
					""",
					(self._stage_id,),
				) as cursor:
					row = await cursor.fetchone()
				if row is None:
					await db.commit()
				else:
					message_id = str(row[0])
					payload_json = str(row[1])
					await db.execute(
						"DELETE FROM queue_messages WHERE message_id = ? AND status = 'queued'",
						(message_id,),
					)
					await db.commit()
					return StateContext.model_validate(orjson.loads(payload_json))
			await asyncio.sleep(0.05)

	async def close(self) -> None:
		return None

	@property
	def depth(self) -> int:
		with sqlite3.connect(self._path) as db:
			row = db.execute(
				"SELECT COUNT(*) FROM queue_messages WHERE stage_name = ? AND status = 'queued'",
				(self._stage_id,),
			).fetchone()
		return int(row[0]) if row is not None else 0


class SQLiteQueueBackend(QueueBackend):
	def __init__(self, path: str) -> None:
		self._path = str(Path(path).expanduser())

	def create_queue(self, stage_id: str) -> QueueAdapter:
		return SQLiteQueueAdapter(path=self._path, stage_id=stage_id)


class SQLiteStoreAdapter:
	def __init__(self, path: str) -> None:
		self._path = str(Path(path).expanduser())
		self._initialized = False

	@property
	def path(self) -> str:
		return self._path

	def _now_iso(self) -> str:
		return datetime.now(timezone.utc).isoformat()

	def _dumps(self, value: Any) -> str:
		return orjson.dumps(value, default=_json_default).decode("utf-8")

	async def _ensure_initialized(self) -> None:
		if self._initialized:
			return
		Path(self._path).parent.mkdir(parents=True, exist_ok=True)
		async with aiosqlite.connect(self._path) as db:
			await db.executescript(
				"""
				CREATE TABLE IF NOT EXISTS pipeline_runs (
				    correlation_id TEXT PRIMARY KEY,
				    status TEXT NOT NULL,
				    reason TEXT NOT NULL DEFAULT '',
				    started_at TEXT NOT NULL,
				    updated_at TEXT NOT NULL
				);
				CREATE TABLE IF NOT EXISTS pipeline_contexts (
				    correlation_id TEXT PRIMARY KEY,
				    context_json TEXT NOT NULL,
				    updated_at TEXT NOT NULL
				);
				CREATE TABLE IF NOT EXISTS queue_messages (
				    message_id TEXT PRIMARY KEY,
				    correlation_id TEXT NOT NULL,
				    stage_name TEXT NOT NULL,
				    payload_json TEXT NOT NULL,
				    status TEXT NOT NULL,
				    visible_after TEXT,
				    created_at TEXT NOT NULL,
				    updated_at TEXT NOT NULL
				);
				CREATE INDEX IF NOT EXISTS idx_queue_messages_stage_status
				    ON queue_messages(stage_name, status);
				CREATE INDEX IF NOT EXISTS idx_queue_messages_correlation
				    ON queue_messages(correlation_id);
				CREATE TABLE IF NOT EXISTS dlq_messages (
				    message_id TEXT PRIMARY KEY,
				    correlation_id TEXT NOT NULL,
				    stage_name TEXT NOT NULL,
				    payload_json TEXT NOT NULL,
				    error TEXT,
				    created_at TEXT NOT NULL
				);
				CREATE INDEX IF NOT EXISTS idx_dlq_messages_correlation
				    ON dlq_messages(correlation_id);
				"""
			)
			await self._migrate_legacy_runtime_tables(db)
			await db.commit()
		self._initialized = True

	async def _migrate_legacy_runtime_tables(self, db: aiosqlite.Connection) -> None:
		for table_name in ("pipeline_runs", "pipeline_stage_records", "pipeline_context_snapshots"):
			async with db.execute(f"PRAGMA table_info({table_name})") as cursor:
				columns = [str(row[1]) for row in await cursor.fetchall()]
			if "correlation_id" in columns or "run_id" not in columns:
				continue
			await db.execute(f"ALTER TABLE {table_name} RENAME COLUMN run_id TO correlation_id")

		# Promote the most recent legacy context snapshot into the canonical pipeline_contexts row.
		async with db.execute(
			"SELECT name FROM sqlite_master WHERE type='table' AND name='pipeline_context_snapshots'"
		) as cursor:
			has_legacy_snapshots = await cursor.fetchone() is not None
		if has_legacy_snapshots:
			await db.execute(
				"""
				INSERT OR REPLACE INTO pipeline_contexts (correlation_id, context_json, updated_at)
				SELECT s.correlation_id, s.context_json, s.created_at
				FROM pipeline_context_snapshots s
				JOIN (
					SELECT correlation_id, MAX(created_at) AS max_created_at
					FROM pipeline_context_snapshots
					GROUP BY correlation_id
				) latest
				ON latest.correlation_id = s.correlation_id
				AND latest.max_created_at = s.created_at
				"""
			)

		for legacy_table in ("pipeline_stage_records", "pipeline_context_snapshots"):
			await db.execute(f"DROP TABLE IF EXISTS {legacy_table}")

	async def save_stage_result(
		self,
		correlation_id: str,
		stage_name: str,
		attempt: int,
		input_payload: Any,
		result: StageResult,
	) -> None:
		await self._ensure_initialized()
		_ = (correlation_id, stage_name, attempt, input_payload, result)

	async def save_context_snapshot(self, context: StateContext) -> None:
		await self._ensure_initialized()
		async with aiosqlite.connect(self._path) as db:
			await db.execute(
				"""
				INSERT INTO pipeline_contexts (correlation_id, context_json, updated_at)
				VALUES (?, ?, ?)
				ON CONFLICT(correlation_id) DO UPDATE SET
					context_json = excluded.context_json,
					updated_at = excluded.updated_at
				""",
				(
					context.correlation_id,
					self._dumps(context.model_dump(mode="json")),
					self._now_iso(),
				),
			)
			await db.commit()

	async def set_status(
		self,
		correlation_id: str,
		status: RunStatus,
		reason: str = "",
	) -> None:
		await self._ensure_initialized()
		now = self._now_iso()
		async with aiosqlite.connect(self._path) as db:
			await db.execute(
				"""
				INSERT INTO pipeline_runs (correlation_id, status, reason, started_at, updated_at)
				VALUES (?, ?, ?, ?, ?)
				ON CONFLICT(correlation_id) DO UPDATE SET
				    status = excluded.status,
				    reason = excluded.reason,
				    updated_at = excluded.updated_at
				""",
				(correlation_id, status.value, reason, now, now),
			)
			await db.commit()

	async def get_status(self, correlation_id: str) -> RunRecord | None:
		await self._ensure_initialized()
		async with aiosqlite.connect(self._path) as db:
			async with db.execute(
				"""
				SELECT correlation_id, status, reason, started_at, updated_at, 0
				FROM pipeline_runs
				WHERE correlation_id = ?
				""",
				(correlation_id,),
			) as cursor:
				row = await cursor.fetchone()
		if row is None:
			return None
		return RunRecord(
			correlation_id=row[0],
			status=RunStatus(row[1]),
			reason=row[2],
			started_at=datetime.fromisoformat(row[3]),
			updated_at=datetime.fromisoformat(row[4]),
			stage_count=row[5],
		)

	async def list_runs(self, limit: int = 100) -> list[RunRecord]:
		await self._ensure_initialized()
		async with aiosqlite.connect(self._path) as db:
			async with db.execute(
				"""
				SELECT correlation_id, status, reason, started_at, updated_at, 0
				FROM pipeline_runs
				ORDER BY updated_at DESC
				LIMIT ?
				""",
				(limit,),
			) as cursor:
				rows = await cursor.fetchall()
		return [
			RunRecord(
				correlation_id=row[0],
				status=RunStatus(row[1]),
				reason=row[2],
				started_at=datetime.fromisoformat(row[3]),
				updated_at=datetime.fromisoformat(row[4]),
				stage_count=row[5],
			)
			for row in rows
		]


@dataclass(frozen=True)
class Topology:
	stages: list[StageNode]
	store_backend: SQLiteStoreAdapter
	queue_backend: QueueBackend | None = None
	gates: dict[str, Gate] = field(default_factory=dict)

	def __post_init__(self) -> None:
		if self.queue_backend is None:
			object.__setattr__(self, "queue_backend", SQLiteQueueBackend(self.store_backend.path))

	def stage_ids(self) -> list[str]:
		return [stage.id for stage in self.stages]


class PipelineRunner:
	def __init__(self, topology: Topology) -> None:
		if not topology.stages:
			raise ValueError("Topology requires at least one stage")
		self._topology = topology
		self._stage_by_id = {stage.id: stage for stage in topology.stages}
		if len(self._stage_by_id) != len(topology.stages):
			raise ValueError("Stage ids must be unique")
		self._order = [stage.id for stage in topology.stages]
		self._next_stage = {
			stage.id: (self._order[index + 1] if index + 1 < len(self._order) else None)
			for index, stage in enumerate(topology.stages)
		}
		self._queues = {
			stage.id: topology.queue_backend.create_queue(stage.id) for stage in topology.stages
		}
		self._worker_tasks: list[asyncio.Task[None]] = []
		self._started_stage_ids: set[str] = set()
		self._run_futures: dict[str, asyncio.Future[RunRecord | None]] = {}

	async def start(self, stage_ids: list[str] | None = None) -> None:
		selected = stage_ids or self._order
		for stage_id in selected:
			if stage_id in self._started_stage_ids:
				continue
			stage = self._stage_by_id[stage_id]
			for worker_index in range(max(1, int(stage.executor.concurrency))):
				task = asyncio.create_task(
					self._worker_loop(stage.id, stage.executor),
					name=f"autorole_next:{stage.id}:{worker_index}",
				)
				self._worker_tasks.append(task)
			self._started_stage_ids.add(stage_id)

	async def shutdown(self) -> None:
		for task in self._worker_tasks:
			task.cancel()
		if self._worker_tasks:
			await asyncio.gather(*self._worker_tasks, return_exceptions=True)
		self._worker_tasks.clear()
		self._started_stage_ids.clear()
		for queue in self._queues.values():
			await queue.close()

	async def run(
		self,
		data: Any,
		*,
		correlation_id: str | None = None,
		metadata: dict[str, Any] | None = None,
	) -> str:
		correlation_id = correlation_id or str(uuid4())
		context = StateContext(
			correlation_id=correlation_id,
			current_stage=self._order[0],
			data=data,
			metadata=dict(metadata or {}),
		)
		loop = asyncio.get_running_loop()
		future = self._run_futures.get(correlation_id)
		if future is None or future.done():
			self._run_futures[correlation_id] = loop.create_future()
		await self._topology.store_backend.set_status(correlation_id, RunStatus.RUNNING)
		await self._topology.store_backend.save_context_snapshot(context)
		await self._queues[self._order[0]].put(context)
		return correlation_id

	async def wait_for_completion(
		self,
		correlation_id: str,
		*,
		timeout: float | None = None,
	) -> RunRecord | None:
		future = self._run_futures.get(correlation_id)
		if future is None:
			return await self._topology.store_backend.get_status(correlation_id)
		if timeout is None:
			return await future
		return await asyncio.wait_for(future, timeout=timeout)

	async def run_until_complete(
		self,
		data: Any,
		*,
		correlation_id: str | None = None,
		metadata: dict[str, Any] | None = None,
		timeout: float | None = None,
	) -> RunRecord | None:
		actual_id = await self.run(data, correlation_id=correlation_id, metadata=metadata)
		return await self.wait_for_completion(actual_id, timeout=timeout)

	async def _worker_loop(self, stage_id: str, executor: Executor) -> None:
		queue = self._queues[stage_id]
		while True:
			context = await queue.get()
			if context.current_stage != stage_id:
				continue
			started = asyncio.get_running_loop().time()
			try:
				result = await executor.execute(context)
			except asyncio.CancelledError:
				raise
			except Exception as exc:
				result = StageResult.fail(str(exc), exc.__class__.__name__)
			duration_ms = (asyncio.get_running_loop().time() - started) * 1000.0
			result = result.model_copy(update={"duration_ms": duration_ms})
			await self._topology.store_backend.save_stage_result(
				context.correlation_id,
				stage_id,
				context.attempt,
				context.data,
				result,
			)
			if not result.success:
				await self._complete_run(context.correlation_id, RunStatus.ERROR, result.error or stage_id)
				continue

			default_next_stage = self._next_stage[stage_id]
			gate = self._topology.gates.get(stage_id, PassThroughGate())
			next_stage = gate.route(result, context, default_next_stage)
			if next_stage is None:
				await self._complete_run(context.correlation_id, RunStatus.COMPLETED)
				continue

			next_context = context.model_copy(
				update={
					"current_stage": next_stage,
					"attempt": 1,
					"data": result.output,
				}
			)
			await self._topology.store_backend.save_context_snapshot(next_context)
			await self._queues[next_stage].put(next_context)

	async def _complete_run(self, correlation_id: str, status: RunStatus, reason: str = "") -> None:
		await self._topology.store_backend.set_status(correlation_id, status, reason)
		record = await self._topology.store_backend.get_status(correlation_id)
		future = self._run_futures.get(correlation_id)
		if future is not None and not future.done():
			future.set_result(record)


class PipelineSeeder(abc.ABC):
	def __init__(self, runner: PipelineRunner) -> None:
		self._runner = runner

	@abc.abstractmethod
	async def seed(self, *args: Any, **kwargs: Any) -> Any:
		...