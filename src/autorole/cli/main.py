from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiosqlite

import typer

from autorole.config import AppConfig
from autorole.context import DiffReport
from autorole.db.repository import JobRepository
from autorole.integrations.credentials import CredentialStore
from autorole.pipeline import build_pipeline
from autorole.pipeline import run_daily

app = typer.Typer(help="AutoRole command line interface")
credentials_app = typer.Typer(help="Manage stored credentials")
app.add_typer(credentials_app, name="credentials")


def _db_path(config: AppConfig) -> Path:
	return Path(config.db_path).expanduser()


def _base_dir(config: AppConfig) -> Path:
	return Path(config.base_dir).expanduser()


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


@app.command()
def tui() -> None:
	from autorole.cli.tui import AutoRoleTUI

	AutoRoleTUI().run()


def main() -> None:
	app()


if __name__ == "__main__":
	main()

