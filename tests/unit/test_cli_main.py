from __future__ import annotations

from pathlib import Path
from typing import Any

from autorole.cli import main as cli


def test_status_no_runs(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
	monkeypatch.setenv("AR_DB_PATH", str(tmp_path / "pipeline.db"))
	cli.status(run_id=None)
	out = capsys.readouterr().out
	assert "No runs found." in out


def test_blocked_no_runs(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
	monkeypatch.setenv("AR_DB_PATH", str(tmp_path / "pipeline.db"))
	cli.blocked()
	out = capsys.readouterr().out
	assert "No blocked/error runs found." in out


def test_diff_no_run(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
	monkeypatch.setenv("AR_DB_PATH", str(tmp_path / "pipeline.db"))
	cli.diff("missing")
	out = capsys.readouterr().out
	assert "No run diff found." in out


def test_score_no_records(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
	monkeypatch.setenv("AR_DB_PATH", str(tmp_path / "pipeline.db"))
	cli.score("missing")
	out = capsys.readouterr().out
	assert "No score records found." in out


def test_prune_no_db(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
	monkeypatch.setenv("AR_DB_PATH", str(tmp_path / "missing.db"))
	cli.prune()
	out = capsys.readouterr().out
	assert "No database found yet." in out


def test_resume_invokes_runner(monkeypatch: Any, capsys: Any) -> None:
	called: dict[str, str] = {}

	class FakeRunner:
		async def resume(self, run_id: str, stage: str) -> None:
			called["run_id"] = run_id
			called["stage"] = stage

	async def fake_build_pipeline(_config: Any) -> tuple[FakeRunner, None]:
		return FakeRunner(), None

	monkeypatch.setattr(cli, "build_pipeline", fake_build_pipeline)
	cli.resume("abc", from_stage="tailoring")
	out = capsys.readouterr().out
	assert "Resume requested" in out
	assert called == {"run_id": "abc", "stage": "tailoring"}


def test_credentials_set_and_delete(monkeypatch: Any, capsys: Any) -> None:
	seen: dict[str, str] = {}

	class FakeStore:
		def set(self, key: str, value: str) -> None:
			seen[f"set:{key}"] = value

		def delete(self, key: str) -> None:
			seen[f"del:{key}"] = "1"

	monkeypatch.setattr(cli, "CredentialStore", FakeStore)
	monkeypatch.setattr(cli.typer, "prompt", lambda *_args, **_kwargs: "secret")

	cli.credentials_set("api_key")
	cli.credentials_delete("api_key")
	out = capsys.readouterr().out
	assert "saved" in out
	assert "deleted" in out
	assert seen["set:api_key"] == "secret"
	assert seen["del:api_key"] == "1"
