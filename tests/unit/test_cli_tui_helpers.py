from __future__ import annotations

import aiosqlite
import pytest

from autorole.cli.tui import _latest_stage_and_status, _table_exists


@pytest.mark.asyncio
async def test_table_exists_helper() -> None:
	async with aiosqlite.connect(":memory:") as db:
		await db.execute("CREATE TABLE demo (id INTEGER)")
		await db.commit()
		assert await _table_exists(db, "demo") is True
		assert await _table_exists(db, "missing") is False


@pytest.mark.asyncio
async def test_latest_stage_and_status_helper() -> None:
	async with aiosqlite.connect(":memory:") as db:
		await db.execute(
			"CREATE TABLE pipeline_stage_records (run_id TEXT, stage_name TEXT, recorded_at TEXT)"
		)
		await db.execute(
			"CREATE TABLE pipeline_runs (run_id TEXT, status TEXT)"
		)
		await db.execute(
			"INSERT INTO pipeline_stage_records (run_id, stage_name, recorded_at) VALUES ('r1', 'scoring', '2026-01-01T00:00:00')"
		)
		await db.execute(
			"INSERT INTO pipeline_runs (run_id, status) VALUES ('r1', 'running')"
		)
		await db.commit()

		stage, status = await _latest_stage_and_status(db, "r1")
		assert stage == "scoring"
		assert status == "running"
