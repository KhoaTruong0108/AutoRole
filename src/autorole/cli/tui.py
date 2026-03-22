from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import aiosqlite

from autorole.config import AppConfig
from autorole.context import DiffReport

try:
	from textual.app import App, ComposeResult
	from textual.binding import Binding
	from textual.containers import Vertical
	from textual.widgets import DataTable, Footer, Header, Markdown, Static, TabbedContent, TabPane

	TEXTUAL_AVAILABLE = True
except Exception:
	TEXTUAL_AVAILABLE = False


if not TEXTUAL_AVAILABLE:
	class AutoRoleTUI:  # type: ignore[no-redef]
		def run(self) -> None:
			print("Textual is not available in this environment.")

else:
	class AutoRoleTUI(App[None]):
		CSS_PATH = None
		BINDINGS = [
			Binding("q", "quit", "Quit"),
			Binding("r", "resume", "Resume"),
			Binding("d", "full_diff", "Full Diff"),
			Binding("s", "open_score", "Open Score"),
		]

		def __init__(self) -> None:
			super().__init__()
			self._config = AppConfig()
			self._runs_table: DataTable | None = None
			self._blocked_table: DataTable | None = None
			self._score_table: DataTable | None = None
			self._detail = Static("Select a run for details")
			self._diff_markdown = Markdown("No diff loaded")

		def compose(self) -> ComposeResult:
			yield Header(show_clock=True)
			with TabbedContent():
				with TabPane("Runs", id="runs"):
					with Vertical():
						table = DataTable(id="runs-table")
						table.add_columns("run_id", "stage", "status", "score")
						self._runs_table = table
						yield table
						yield self._detail
				with TabPane("Blocked", id="blocked"):
					table = DataTable(id="blocked-table")
					table.add_columns("run_id", "status", "reason")
					self._blocked_table = table
					yield table
				with TabPane("Score", id="score"):
					table = DataTable(id="score-table")
					table.add_columns("criterion", "score", "matched", "gaps")
					self._score_table = table
					yield table
				with TabPane("Diff", id="diff"):
					yield self._diff_markdown
				with TabPane("Config", id="config"):
					yield Static(
						f"db_path={self._config.db_path}\n"
						f"resume_dir={self._config.resume_dir}\n"
						f"master_resume={self._config.master_resume}"
					)
			yield Footer()

		async def on_mount(self) -> None:
			await self.refresh_runs()
			self.set_interval(5, self.refresh_runs)

		async def refresh_runs(self) -> None:
			db_path = Path(self._config.db_path).expanduser()
			if not db_path.exists():
				return

			async with aiosqlite.connect(db_path) as db:
				if self._runs_table is not None:
					self._runs_table.clear(columns=False)
					if await _table_exists(db, "job_applications"):
						async with db.execute(
							"SELECT run_id, overall_score FROM job_applications ORDER BY created_at DESC LIMIT 100"
						) as cur:
							for run_id, score in await cur.fetchall():
								stage, status = await _latest_stage_and_status(db, run_id)
								self._runs_table.add_row(
									run_id,
									stage,
									status,
									"-" if score is None else f"{score:.3f}",
								)

				if self._blocked_table is not None:
					self._blocked_table.clear(columns=False)
					if await _table_exists(db, "pipeline_runs"):
						async with db.execute(
							"SELECT run_id, status, reason FROM pipeline_runs "
							"WHERE status IN ('blocked', 'error') ORDER BY updated_at DESC"
						) as cur:
							for run_id, status, reason in await cur.fetchall():
								self._blocked_table.add_row(run_id, status, reason or "-")

		async def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
			table = event.data_table
			if table is not self._runs_table:
				return
			row_key = event.row_key
			if row_key is None:
				return
			row = table.get_row(row_key)
			run_id = str(row[0])
			self._detail.update(f"Selected run: {run_id}")
			await self._load_score_and_diff(run_id)

		async def _load_score_and_diff(self, run_id: str) -> None:
			db_path = Path(self._config.db_path).expanduser()
			if not db_path.exists():
				return
			async with aiosqlite.connect(db_path) as db:
				if self._score_table is not None:
					self._score_table.clear(columns=False)
					if await _table_exists(db, "score_reports"):
						async with db.execute(
							"SELECT criteria_scores, matched, mismatched FROM score_reports "
							"WHERE run_id = ? ORDER BY attempt DESC, id DESC LIMIT 1",
							(run_id,),
						) as cur:
							row = await cur.fetchone()
							if row:
								criteria = json.loads(row[0]) if row[0] else {}
								matched = json.loads(row[1]) if row[1] else []
								gaps = json.loads(row[2]) if row[2] else []
								for criterion, score in criteria.items():
									self._score_table.add_row(
										criterion,
										f"{float(score):.3f}",
										", ".join(matched) if matched else "-",
										", ".join(gaps) if gaps else "-",
									)

				if await _table_exists(db, "tailored_resumes"):
					async with db.execute(
						"SELECT diff_summary FROM tailored_resumes WHERE run_id = ? "
						"ORDER BY tailored_at DESC LIMIT 1",
						(run_id,),
					) as cur:
						row = await cur.fetchone()
						if row and row[0]:
							try:
								report = DiffReport.model_validate(json.loads(row[0]))
								self._diff_markdown.update(report.to_brief())
							except Exception:
								self._diff_markdown.update("Unable to parse diff report")

		def action_resume(self) -> None:
			self.notify("Resume action is available via CLI command: ar resume <run_id>")

		def action_full_diff(self) -> None:
			self.notify("Use CLI: ar diff <run_id> --full")

		def action_open_score(self) -> None:
			self.notify("Use CLI: ar score <run_id>")


async def _table_exists(db: aiosqlite.Connection, table_name: str) -> bool:
	async with db.execute(
		"SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
		(table_name,),
	) as cur:
		return (await cur.fetchone()) is not None


async def _latest_stage_and_status(db: aiosqlite.Connection, run_id: str) -> tuple[str, str]:
	stage = "unknown"
	status = "unknown"
	if await _table_exists(db, "pipeline_stage_records"):
		async with db.execute(
			"SELECT stage_name FROM pipeline_stage_records WHERE run_id = ? "
			"ORDER BY recorded_at DESC LIMIT 1",
			(run_id,),
		) as cur:
			row = await cur.fetchone()
			if row:
				stage = row[0]

	if await _table_exists(db, "pipeline_runs"):
		async with db.execute("SELECT status FROM pipeline_runs WHERE run_id = ?", (run_id,)) as cur:
			row = await cur.fetchone()
			if row:
				status = row[0]
	return stage, status

