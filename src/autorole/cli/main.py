from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import aiosqlite

import typer

from autorole.config import AppConfig
from autorole.context import DiffReport
from autorole.db.repository import JobRepository
from autorole.integrations.credentials import CredentialStore
from autorole.pipeline import build_pipeline
from autorole.pipeline import run_daily
from autorole.queue import (
	CONCLUDING_Q,
	DEAD_LETTER_Q,
	EXPLORING_Q,
	FORM_INTEL_Q,
	FORM_SUB_Q,
	LLM_FIELD_COMPLETER_Q,
	PACKAGING_Q,
	SCORING_Q,
	TAILORING_Q,
	SESSION_Q,
)

app = typer.Typer(help="AutoRole command line interface")
credentials_app = typer.Typer(help="Manage stored credentials")
queue_app = typer.Typer(help="Inspect persisted SQLite queue messages")
app.add_typer(credentials_app, name="credentials")
app.add_typer(queue_app, name="queue")


def _db_path(config: AppConfig) -> Path:
	return Path(config.db_path).expanduser()


def _base_dir(config: AppConfig) -> Path:
	return Path(config.base_dir).expanduser()


def _sqlite_string_literal(value: str) -> str:
	return "'" + value.replace("'", "''") + "'"


def _option_value(value: Any) -> Any:
	default = getattr(value, "default", value)
	return default

'''
PYTHONPATH=src /Users/khoatruong0108/workspace/AutoRole/.venv/bin/python -m autorole queue sql scoring_q --payload
PYTHONPATH=src /Users/khoatruong0108/workspace/AutoRole/.venv/bin/python -m autorole queue sql scoring_q --run-id your-run-id
PYTHONPATH=src /Users/khoatruong0108/workspace/AutoRole/.venv/bin/python -m autorole queue sql dead_letter_q --all --payload --limit 50
PYTHONPATH=src /Users/khoatruong0108/workspace/AutoRole/.venv/bin/python -m autorole queue sql scoring_q --message-id your-message-id
'''
def _build_queue_sql(
	queue_name: str,
	*,
	run_id: str = "",
	message_id: str = "",
	visible_only: bool = True,
	include_payload: bool = False,
	limit: int = 20,
) -> str:
	select_columns = [
		"message_id",
		"queue_name",
		"run_id",
		"stage",
		"attempt",
		"reply_queue",
		"dead_letter_queue",
		"status",
		"enqueued_at",
		"visible_after",
	]
	if include_payload:
		select_columns.extend(["payload", "metadata"])

	where_clauses = [f"queue_name = {_sqlite_string_literal(queue_name)}"]
	if visible_only:
		where_clauses.append("status IN ('queued', 'pending')")
		where_clauses.append("visible_after <= datetime('now')")
	if run_id.strip():
		where_clauses.append(f"run_id = {_sqlite_string_literal(run_id.strip())}")
	if message_id.strip():
		where_clauses.append(f"message_id = {_sqlite_string_literal(message_id.strip())}")

	limit_value = max(1, limit)
	return (
		"SELECT\n"
		+ "\n".join(f"  {column}{',' if index < len(select_columns) - 1 else ''}" for index, column in enumerate(select_columns))
		+ "\nFROM queue_messages\n"
		+ "WHERE\n  "
		+ "\n  AND ".join(where_clauses)
		+ "\nORDER BY enqueued_at ASC\n"
		+ f"LIMIT {limit_value};"
	)


def _stage_input_queue(stage_name: str) -> str | None:
	mapping = {
		"exploring": EXPLORING_Q,
		"scoring": SCORING_Q,
		"tailoring": TAILORING_Q,
		"packaging": PACKAGING_Q,
		"session": SESSION_Q,
		"form_intelligence": FORM_INTEL_Q,
		"llm_field_completer": LLM_FIELD_COMPLETER_Q,
		"form_submission": FORM_SUB_Q,
		"concluding": CONCLUDING_Q,
	}
	return mapping.get(stage_name.strip().lower())


def _redriveable_queue_names() -> tuple[str, ...]:
	return (
		EXPLORING_Q,
		SCORING_Q,
		TAILORING_Q,
		PACKAGING_Q,
		SESSION_Q,
		FORM_INTEL_Q,
		LLM_FIELD_COMPLETER_Q,
		FORM_SUB_Q,
		CONCLUDING_Q,
	)


def _sanitize_redrive_metadata(raw_metadata: str) -> str:
	try:
		decoded = json.loads(raw_metadata or "{}")
	except json.JSONDecodeError:
		decoded = {}
	if not isinstance(decoded, dict):
		decoded = {}
	decoded.pop("__exec_attempt", None)
	decoded.pop("__loop_attempt", None)
	return json.dumps(decoded, separators=(",", ":"), ensure_ascii=False)


async def _redrive_dlq_messages(
	path: Path,
	*,
	message_id: str = "",
	queue_name: str = "",
) -> list[tuple[str, str, str]]:
	results: list[tuple[str, str, str]] = []
	async with aiosqlite.connect(path) as db:
		if not await _table_exists(db, "queue_messages"):
			return results

		async with db.execute(
			"""
			SELECT
				message_id,
				run_id,
				stage,
				payload,
				attempt,
				reply_queue,
				dead_letter_queue,
				metadata
			FROM queue_messages
			WHERE queue_name = ?
			ORDER BY enqueued_at ASC
			""",
			(DEAD_LETTER_Q,),
		) as cur:
			rows = await cur.fetchall()

		selected: list[tuple[str, str, str, str, int, str, str, str, str]] = []
		for row in rows:
			current_message_id = str(row[0])
			stage_name = str(row[2])
			target_queue = _stage_input_queue(stage_name)
			if target_queue is None:
				continue
			if message_id and current_message_id != message_id:
				continue
			if queue_name and target_queue != queue_name:
				continue
			selected.append(
				(
					current_message_id,
					str(row[1]),
					stage_name,
					str(row[3]),
					int(row[4]),
					str(row[5]),
					str(row[6]),
					str(row[7] or "{}"),
					target_queue,
				)
			)

		if not selected:
			return results

		now_iso = datetime.now(timezone.utc).isoformat()
		await db.execute("BEGIN IMMEDIATE")
		try:
			for old_message_id, run_id, stage_name, payload, _attempt, reply_queue, dead_letter_queue, metadata, target_queue in selected:
				new_message_id = str(uuid4())
				await db.execute(
					"""
					INSERT INTO queue_messages (
						message_id,
						queue_name,
						run_id,
						stage,
						payload,
						attempt,
						reply_queue,
						dead_letter_queue,
						metadata,
						status,
						enqueued_at,
						visible_after
					) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
					""",
					(
						new_message_id,
						target_queue,
						run_id,
						stage_name,
						payload,
						1,
						reply_queue,
						dead_letter_queue,
						_sanitize_redrive_metadata(metadata),
						now_iso,
						now_iso,
					),
				)
				await db.execute("DELETE FROM queue_messages WHERE message_id = ?", (old_message_id,))
				results.append((old_message_id, new_message_id, target_queue))
			await db.commit()
		except Exception:
			await db.rollback()
			raise

	return results


async def _ensure_db(path: Path) -> None:
	path.parent.mkdir(parents=True, exist_ok=True)
	if path.exists():
		return
	async with aiosqlite.connect(path):
		pass


async def _table_exists(db: aiosqlite.Connection, name: str) -> bool:
	async with db.execute(
		"SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
		(name,),
	) as cur:
		return (await cur.fetchone()) is not None


async def _iter_recent_runs(db: aiosqlite.Connection) -> list[tuple[str, str, str, str]]:
	if not await _table_exists(db, "job_applications"):
		return []

	latest_stage: dict[str, str] = {}
	run_status: dict[str, str] = {}
	if await _table_exists(db, "pipeline_stage_records"):
		async with db.execute(
			"SELECT run_id, stage_name FROM pipeline_stage_records ORDER BY recorded_at DESC"
		) as cur:
			rows = await cur.fetchall()
		for run_id, stage_name in rows:
			latest_stage.setdefault(run_id, stage_name)

	if await _table_exists(db, "pipeline_runs"):
		async with db.execute("SELECT run_id, status FROM pipeline_runs") as cur:
			for run_id, status in await cur.fetchall():
				run_status[run_id] = status

	async with db.execute(
		"SELECT run_id, overall_score FROM job_applications ORDER BY created_at DESC LIMIT 100"
	) as cur:
		rows = await cur.fetchall()

	result: list[tuple[str, str, str, str]] = []
	for run_id, overall_score in rows:
		stage = latest_stage.get(run_id, "unknown")
		status = run_status.get(run_id, "unknown")
		score = "-" if overall_score is None else f"{overall_score:.3f}"
		result.append((run_id, stage, status, score))
	return result


def _render_score_table(criteria_scores: dict[str, Any], matched: list[str], gaps: list[str]) -> None:
	try:
		from rich.console import Console
		from rich.table import Table

		table = Table(title="Score Breakdown")
		table.add_column("Criterion")
		table.add_column("Score", justify="right")
		table.add_column("Matched")
		table.add_column("Gaps")
		for criterion, value in criteria_scores.items():
			table.add_row(
				criterion,
				f"{float(value):.3f}",
				", ".join(matched) if matched else "-",
				", ".join(gaps) if gaps else "-",
			)
		Console().print(table)
	except Exception:
		typer.echo("Criterion               Score")
		for criterion, value in criteria_scores.items():
			typer.echo(f"{criterion:22} {float(value):.3f}")
		typer.echo(f"Matched: {', '.join(matched) if matched else '-'}")
		typer.echo(f"Gaps: {', '.join(gaps) if gaps else '-'}")


@app.command()
def run() -> None:
	asyncio.run(run_daily(AppConfig()))
	typer.echo("AutoRole run completed")


@app.command()
def status(run_id: str | None = typer.Argument(default=None)) -> None:
	async def _inner() -> None:
		config = AppConfig()
		path = _db_path(config)
		await _ensure_db(path)

		async with aiosqlite.connect(path) as db:
			if run_id is None:
				rows = await _iter_recent_runs(db)
				if not rows:
					typer.echo("No runs found.")
					return
				typer.echo("run_id | stage | status | score")
				for row in rows:
					typer.echo(" | ".join(row))
				return

			if await _table_exists(db, "pipeline_stage_records"):
				typer.echo("Stage records:")
				async with db.execute(
					"SELECT stage_name, attempt, success, error_type, recorded_at "
					"FROM pipeline_stage_records WHERE run_id = ? ORDER BY recorded_at ASC",
					(run_id,),
				) as cur:
					rows = await cur.fetchall()
					for stage_name, attempt, success, error_type, recorded_at in rows:
						typer.echo(
							f"- {recorded_at} stage={stage_name} attempt={attempt} "
							f"success={bool(success)} error_type={error_type or '-'}"
						)

			if await _table_exists(db, "job_applications"):
				typer.echo("Domain record:")
				async with db.execute(
					"SELECT run_id, submission_status, overall_score, tailoring_degree, applied_at "
					"FROM job_applications WHERE run_id = ?",
					(run_id,),
				) as cur:
					row = await cur.fetchone()
					if row:
						typer.echo(
							f"run_id={row[0]} submission_status={row[1]} "
							f"overall_score={row[2]} tailoring_degree={row[3]} applied_at={row[4]}"
						)
					else:
						typer.echo("No domain record found for this run.")

	asyncio.run(_inner())


@app.command()
def blocked() -> None:
	async def _inner() -> None:
		config = AppConfig()
		path = _db_path(config)
		await _ensure_db(path)
		async with aiosqlite.connect(path) as db:
			if not await _table_exists(db, "pipeline_runs"):
				typer.echo("No blocked/error runs found.")
				return
			async with db.execute(
				"SELECT run_id, status, reason, updated_at FROM pipeline_runs "
				"WHERE status IN ('blocked', 'error') ORDER BY updated_at DESC"
			) as cur:
				rows = await cur.fetchall()
				if not rows:
					typer.echo("No blocked/error runs found.")
					return
				for run_id, status_value, reason, updated_at in rows:
					typer.echo(f"{updated_at} {run_id} status={status_value} reason={reason or '-'}")

	asyncio.run(_inner())


@app.command()
def resume(
	run_id: str,
	from_stage: str | None = typer.Option(default=None, help="Stage name to resume from"),
) -> None:
	async def _inner() -> None:
		runner, _exploring = await build_pipeline(AppConfig())
		if not hasattr(runner, "resume"):
			typer.echo("Resume is not available with the current pipeline backend.")
			return
		stage_name = from_stage or "scoring"
		await runner.resume(run_id, stage_name)
		typer.echo(f"Resume requested for run_id={run_id} from_stage={stage_name}")

	asyncio.run(_inner())


@app.command()
def diff(run_id: str, full: bool = typer.Option(False, "--full", help="Show full diff")) -> None:
	async def _inner() -> None:
		config = AppConfig()
		path = _db_path(config)
		await _ensure_db(path)
		async with aiosqlite.connect(path) as db:
			if not await _table_exists(db, "tailored_resumes"):
				typer.echo("No run diff found.")
				return
			async with db.execute(
				"SELECT diff_summary FROM tailored_resumes WHERE run_id = ? "
				"ORDER BY tailored_at DESC LIMIT 1",
				(run_id,),
			) as cur:
				row = await cur.fetchone()
				if not row or not row[0]:
					typer.echo("No run diff found.")
					return
				report = DiffReport.model_validate(json.loads(row[0]))
				typer.echo(report.to_full() if full else report.to_brief())

	asyncio.run(_inner())


@app.command()
def score(run_id: str) -> None:
	async def _inner() -> None:
		config = AppConfig()
		path = _db_path(config)
		await _ensure_db(path)
		async with aiosqlite.connect(path) as db:
			if not await _table_exists(db, "score_reports"):
				typer.echo("No score records found.")
				return
			async with db.execute(
				"SELECT overall_score, criteria_scores, matched, mismatched "
				"FROM score_reports WHERE run_id = ? ORDER BY attempt DESC, id DESC LIMIT 1",
				(run_id,),
			) as cur:
				row = await cur.fetchone()
				if not row:
					typer.echo("No score found for run.")
					return
				overall, criteria_raw, matched_raw, mismatched_raw = row
				criteria = json.loads(criteria_raw) if criteria_raw else {}
				matched = json.loads(matched_raw) if matched_raw else []
				mismatched = json.loads(mismatched_raw) if mismatched_raw else []
				typer.echo(f"Overall score: {overall}")
				_render_score_table(criteria, matched, mismatched)

	asyncio.run(_inner())


@credentials_app.command("set")
def credentials_set(key: str) -> None:
	value = typer.prompt(f"Value for {key}", hide_input=True)
	CredentialStore().set(key, value)
	typer.echo(f"Credential '{key}' saved")


@credentials_app.command("delete")
def credentials_delete(key: str) -> None:
	CredentialStore().delete(key)
	typer.echo(f"Credential '{key}' deleted")


@app.command()
def prune() -> None:
	async def _inner() -> None:
		config = AppConfig()
		base = _base_dir(config)
		base.mkdir(parents=True, exist_ok=True)
		path = _db_path(config)
		if not path.exists():
			typer.echo("No database found yet.")
			return
		async with aiosqlite.connect(path) as db:
			repo = JobRepository(db)
			files = await repo.get_pruneable_files(config.retention.max_age_days)
			deleted = 0
			for file_path in files:
				try:
					Path(file_path).unlink()
					deleted += 1
				except FileNotFoundError:
					continue
			typer.echo(f"Pruned {deleted} file(s)")

	asyncio.run(_inner())


@queue_app.command("sql")
def queue_sql(
	queue_name: str = typer.Argument(..., help="Queue name, e.g. scoring_q"),
	run_id: str = typer.Option(default="", help="Filter by run_id"),
	message_id: str = typer.Option(default="", help="Filter by exact message_id"),
	visible_only: bool = typer.Option(True, "--visible-only/--all", help="Only include currently visible queued messages"),
	include_payload: bool = typer.Option(False, "--payload", help="Include payload and metadata JSON columns"),
	limit: int = typer.Option(20, min=1, help="Maximum rows to select"),
) -> None:
	run_id = str(_option_value(run_id) or "")
	message_id = str(_option_value(message_id) or "")
	visible_only = bool(_option_value(visible_only))
	include_payload = bool(_option_value(include_payload))
	limit = int(_option_value(limit) or 20)
	config = AppConfig()
	path = _db_path(config)
	query = _build_queue_sql(
		queue_name,
		run_id=run_id,
		message_id=message_id,
		visible_only=visible_only,
		include_payload=include_payload,
		limit=limit,
	)
	typer.echo(f"# DB: {path}")
	typer.echo(query)


@queue_app.command("redrive")
def queue_redrive(
	message_id: str = typer.Option(default="", help="Redrive one dead-letter message by exact message_id"),
	queue_name: str = typer.Option(
		default="",
		help="Redrive all dead-letter messages whose canonical input queue matches this queue name, e.g. scoring_q",
	),
) -> None:
	message_id = str(_option_value(message_id) or "").strip()
	queue_name = str(_option_value(queue_name) or "").strip()
	if bool(message_id) == bool(queue_name):
		raise typer.BadParameter("Provide exactly one of --message-id or --queue-name")
	if queue_name and queue_name not in _redriveable_queue_names():
		raise typer.BadParameter(
			f"queue_name must be one of: {', '.join(_redriveable_queue_names())}"
		)

	config = AppConfig()
	path = _db_path(config)
	if not path.exists():
		typer.echo("No database found yet.")
		return

	redriven = asyncio.run(_redrive_dlq_messages(path, message_id=message_id, queue_name=queue_name))
	if not redriven:
		if message_id:
			typer.echo(f"No dead-letter message found for message_id={message_id}.")
			return
		typer.echo(f"No dead-letter messages found for queue_name={queue_name}.")
		return

	if message_id:
		old_message_id, new_message_id, target_queue = redriven[0]
		typer.echo(
			f"Redrove dead-letter message {old_message_id} to {target_queue} as {new_message_id}."
		)
		return

	typer.echo(f"Redrove {len(redriven)} dead-letter message(s) to {queue_name}.")


@app.command()
def tui() -> None:
	from autorole.cli.tui import AutoRoleTUI

	AutoRoleTUI().run()


def main() -> None:
	app()


if __name__ == "__main__":
	main()

