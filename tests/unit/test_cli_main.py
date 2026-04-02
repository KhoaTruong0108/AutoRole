from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from autorole.cli import main as cli


def _init_queue_db(path: Path) -> None:
	conn = sqlite3.connect(path)
	try:
		root = Path(__file__).resolve().parents[2]
		domain_schema = (root / "src/autorole/db/migrations/001_domain.sql").read_text(encoding="utf-8")
		queue_schema = (root / "src/autorole/db/migrations/002_queue.sql").read_text(encoding="utf-8")
		conn.executescript(domain_schema)
		conn.executescript(queue_schema)
		conn.commit()
	finally:
		conn.close()


def _insert_dlq_message(
	path: Path,
	*,
	message_id: str,
	run_id: str,
	stage: str,
	reply_queue: str,
	metadata: dict[str, Any] | None = None,
) -> None:
	conn = sqlite3.connect(path)
	try:
		payload = json.dumps({"run_id": run_id, "stage": stage})
		encoded_metadata = json.dumps(metadata or {})
		now = "2026-03-30T00:00:00+00:00"
		conn.execute(
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
			) VALUES (?, 'dead_letter_q', ?, ?, ?, 3, ?, 'dead_letter_q', ?, 'queued', ?, ?)
			""",
			(message_id, run_id, stage, payload, reply_queue, encoded_metadata, now, now),
		)
		conn.commit()
	finally:
		conn.close()


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


def test_queue_sql_defaults(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
	monkeypatch.setenv("AR_DB_PATH", str(tmp_path / "pipeline.db"))
	cli.queue_sql("scoring_q")
	out = capsys.readouterr().out
	assert "# DB:" in out
	assert "FROM queue_messages" in out
	assert "queue_name = 'scoring_q'" in out
	assert "status IN ('queued', 'pending')" in out
	assert "visible_after <= datetime('now')" in out
	assert "LIMIT 20;" in out


def test_queue_sql_with_filters_and_payload(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
	monkeypatch.setenv("AR_DB_PATH", str(tmp_path / "pipeline.db"))
	cli.queue_sql(
		"dead_letter_q",
		run_id="run-123",
		message_id="msg-1",
		visible_only=False,
		include_payload=True,
		limit=5,
	)
	out = capsys.readouterr().out
	assert "queue_name = 'dead_letter_q'" in out
	assert "run_id = 'run-123'" in out
	assert "message_id = 'msg-1'" in out
	assert "payload" in out
	assert "metadata" in out
	assert "status IN ('queued', 'pending')" not in out
	assert "LIMIT 5;" in out


def test_queue_redrive_single_message(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
	db_path = tmp_path / "pipeline.db"
	_init_queue_db(db_path)
	_insert_dlq_message(
		db_path,
		message_id="msg-1",
		run_id="run-1",
		stage="scoring",
		reply_queue="packaging_q",
		metadata={"__exec_attempt": 3, "note": "keep"},
	)
	monkeypatch.setenv("AR_DB_PATH", str(db_path))

	cli.queue_redrive(message_id="msg-1")
	out = capsys.readouterr().out
	assert "Redrove dead-letter message msg-1 to scoring_q as" in out

	conn = sqlite3.connect(db_path)
	try:
		rows = conn.execute(
			"SELECT message_id, queue_name, attempt, metadata FROM queue_messages ORDER BY enqueued_at ASC"
		).fetchall()
	finally:
		conn.close()

	assert len(rows) == 1
		
	new_message_id, queue_name, attempt, metadata = rows[0]
	assert new_message_id != "msg-1"
	assert queue_name == "scoring_q"
	assert attempt == 1
	decoded_metadata = json.loads(metadata)
	assert decoded_metadata == {"note": "keep"}


def test_queue_redrive_whole_queue(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
	db_path = tmp_path / "pipeline.db"
	_init_queue_db(db_path)
	_insert_dlq_message(
		db_path,
		message_id="msg-qualification",
		run_id="run-qualification",
		stage="scoring",
		reply_queue="packaging_q",
	)
	_insert_dlq_message(
		db_path,
		message_id="msg-packaging",
		run_id="run-packaging",
		stage="packaging",
		reply_queue="session_q",
	)
	monkeypatch.setenv("AR_DB_PATH", str(db_path))

	cli.queue_redrive(queue_name="scoring_q")
	out = capsys.readouterr().out
	assert "Redrove 1 dead-letter message(s) to scoring_q." in out

	conn = sqlite3.connect(db_path)
	try:
		rows = conn.execute(
			"SELECT queue_name, run_id, stage FROM queue_messages ORDER BY run_id ASC"
		).fetchall()
	finally:
		conn.close()

	assert rows == [
		("dead_letter_q", "run-packaging", "packaging"),
		("scoring_q", "run-qualification", "scoring"),
	]


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
