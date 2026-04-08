from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from autorole_next.cli.main import app

cli_main_module = importlib.import_module("autorole_next.cli.main")


def test_run_seed_command_creates_listing_and_queue_message(tmp_path: Path) -> None:
    db_path = tmp_path / "autorole-next.db"
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "run",
            "seed",
            "--db",
            str(db_path),
            "--job-url",
            "https://example.com/jobs/manual-1",
            "--company-name",
            "Acme",
            "--job-title",
            "Platform Engineer",
            "--platform",
            "workday",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload[0]["status"] == "seeded"

    with sqlite3.connect(db_path) as db:
        queue_row = db.execute(
            "SELECT queue_name, correlation_id FROM queue_messages"
        ).fetchone()
        listing_row = db.execute(
            "SELECT company_name, job_title, platform FROM listings"
        ).fetchone()

    assert queue_row is not None
    assert queue_row[0] == "scoring"
    assert listing_row == ("Acme", "Platform Engineer", "workday")


def test_tui_command_delegates_to_launcher(monkeypatch) -> None:
    runner = CliRunner()
    seen: dict[str, str] = {}

    def fake_launch_tui(db_path: str) -> None:
        seen["db_path"] = db_path

    monkeypatch.setattr(cli_main_module, "launch_tui", fake_launch_tui)

    result = runner.invoke(app, ["tui", "--db", "tmp/manual-seeder.db"])

    assert result.exit_code == 0
    assert seen == {"db_path": "tmp/manual-seeder.db"}


def test_run_stage_command_processes_pending_scoring_message(tmp_path: Path) -> None:
    db_path = tmp_path / "autorole-next.db"
    runner = CliRunner()

    seed_result = runner.invoke(
        app,
        [
            "run",
            "seed",
            "--db",
            str(db_path),
            "--job-url",
            "https://example.com/jobs/manual-2",
            "--company-name",
            "Acme",
            "--job-title",
            "Platform Engineer",
            "--platform",
            "workday",
            "--metadata-json",
            '{"forced_score": 0.93}',
        ],
    )
    assert seed_result.exit_code == 0

    stage_result = runner.invoke(
        app,
        [
            "run",
            "stage",
            "--stage",
            "scoring",
            "--db",
            str(db_path),
            "--max-seconds",
            "10",
        ],
    )

    assert stage_result.exit_code == 0
    payload = json.loads(stage_result.stdout)
    assert payload["status"] in {"drained", "timeout"}

    with sqlite3.connect(db_path) as db:
        queue_depth = db.execute("SELECT COUNT(*) FROM queue_messages WHERE queue_name = 'scoring'").fetchone()
        score_count = db.execute("SELECT COUNT(*) FROM score_reports").fetchone()

    assert queue_depth is not None and int(queue_depth[0]) == 0
    assert score_count is not None and int(score_count[0]) >= 1


def test_run_stage_command_uses_longer_default_for_llm_applying(monkeypatch) -> None:
    runner = CliRunner()
    seen: dict[str, object] = {}

    async def fake_run_stage_worker(**kwargs):
        seen.update(kwargs)
        return {"stage": kwargs["stage"], "status": "drained"}

    monkeypatch.setattr(cli_main_module, "_run_stage_worker", fake_run_stage_worker)

    result = runner.invoke(
        app,
        [
            "run",
            "stage",
            "--stage",
            "llm_applying",
            "--db",
            "tmp/manual-seeder.db",
        ],
    )

    assert result.exit_code == 0
    assert seen["stage"] == "llm_applying"
    assert seen["max_seconds"] is None
    assert cli_main_module._resolve_stage_max_seconds("llm_applying", None) == 900


def test_run_stage_command_preserves_explicit_max_seconds(monkeypatch) -> None:
    runner = CliRunner()
    seen: dict[str, object] = {}

    async def fake_run_stage_worker(**kwargs):
        seen.update(kwargs)
        return {"stage": kwargs["stage"], "status": "drained"}

    monkeypatch.setattr(cli_main_module, "_run_stage_worker", fake_run_stage_worker)

    result = runner.invoke(
        app,
        [
            "run",
            "stage",
            "--stage",
            "llm_applying",
            "--db",
            "tmp/manual-seeder.db",
            "--max-seconds",
            "30",
        ],
    )

    assert result.exit_code == 0
    assert seen["max_seconds"] == 30
    assert cli_main_module._resolve_stage_max_seconds("llm_applying", 30) == 30


def test_run_stage_command_scoring_keeps_short_default(monkeypatch) -> None:
    runner = CliRunner()
    seen: dict[str, object] = {}

    async def fake_run_stage_worker(**kwargs):
        seen.update(kwargs)
        return {"stage": kwargs["stage"], "status": "drained"}

    monkeypatch.setattr(cli_main_module, "_run_stage_worker", fake_run_stage_worker)

    result = runner.invoke(
        app,
        [
            "run",
            "stage",
            "--stage",
            "scoring",
            "--db",
            "tmp/manual-seeder.db",
        ],
    )

    assert result.exit_code == 0
    assert seen["max_seconds"] is None
    assert cli_main_module._resolve_stage_max_seconds("scoring", None) == 300


def test_configured_stage_ids_reflect_topology(tmp_path: Path) -> None:
    db_path = tmp_path / "autorole-next.db"

    stages = cli_main_module._configured_stage_ids(str(db_path))

    assert stages == [
        "scoring",
        "tailoring",
        "packaging",
        "session",
        "formScraper",
        "fieldCompleter",
        "formSubmission",
        "concluding",
    ]


def test_run_all_command_delegates_to_all_stage_worker(monkeypatch) -> None:
    runner = CliRunner()
    seen: dict[str, object] = {}

    async def fake_run_all_stage_workers(**kwargs):
        seen.update(kwargs)
        return {"stages": ["scoring"], "status": "drained"}

    monkeypatch.setattr(cli_main_module, "_run_all_stage_workers", fake_run_all_stage_workers)

    result = runner.invoke(
        app,
        [
            "run",
            "all",
            "--db",
            "tmp/manual-seeder.db",
            "--max-seconds",
            "30",
        ],
    )

    assert result.exit_code == 0
    assert seen == {
        "db_path": "tmp/manual-seeder.db",
        "watch": False,
        "poll_seconds": 1,
        "idle_rounds": 5,
        "max_seconds": 30,
    }
    payload = json.loads(result.stdout)
    assert payload["status"] == "drained"