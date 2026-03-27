#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Iterable

TOP_LEVEL_KEYS = [
    "run_id",
    "started_at",
    "listing",
    "score",
    "tailored",
    "packaged",
    "session",
    "form_intelligence",
    "form_session",
    "applied",
]

FIXTURE_FIELD_MAP: dict[str, list[str]] = {
    "qualification_input.json": ["run_id", "started_at", "listing"],
    "packaging_input.json": ["run_id", "started_at", "listing", "score", "tailored"],
    "session_input.json": ["run_id", "started_at", "listing", "score", "tailored", "packaged"],
    "form_intelligence_input.json": [
        "run_id",
        "started_at",
        "listing",
        "score",
        "tailored",
        "packaged",
        "session",
    ],
    "form_submission_input.json": [
        "run_id",
        "started_at",
        "listing",
        "score",
        "tailored",
        "packaged",
        "session",
        "form_intelligence",
        "form_session",
    ],
    "concluding_input.json": TOP_LEVEL_KEYS,
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _run_real_pipeline(job_url: str, job_platform: str, mode: str, headless: bool) -> None:
    repo_root = _repo_root()
    script_path = repo_root / "scripts" / "run_real_pipeline.py"

    cmd = [sys.executable, str(script_path), "--mode", mode, "--job-url", job_url]
    if job_platform:
        cmd.extend(["--job-platform", job_platform])
    if headless:
        cmd.append("--headless")

    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = "src" if not existing_pythonpath else f"src:{existing_pythonpath}"

    print("[run]", " ".join(cmd))
    result = subprocess.run(cmd, cwd=repo_root, env=env, check=False)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def _resolve_run_id(conn: sqlite3.Connection, explicit_run_id: str, job_url: str) -> str:
    if explicit_run_id:
        return explicit_run_id

    if job_url and _table_exists(conn, "job_listings") and _table_exists(conn, "pipeline_checkpoints"):
        row = conn.execute(
            """
            SELECT pc.run_id
            FROM pipeline_checkpoints pc
            JOIN job_listings jl ON jl.run_id = pc.run_id
            WHERE jl.job_url = ?
            ORDER BY pc.updated_at DESC
            LIMIT 1
            """,
            (job_url,),
        ).fetchone()
        if row:
            return str(row[0])

    if _table_exists(conn, "pipeline_checkpoints"):
        row = conn.execute(
            "SELECT run_id FROM pipeline_checkpoints ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if row:
            return str(row[0])

    raise RuntimeError("No checkpoint found. Run the real pipeline first or pass --run-id.")


def _load_checkpoint_context(conn: sqlite3.Connection, run_id: str) -> dict:
    if not _table_exists(conn, "pipeline_checkpoints"):
        raise RuntimeError("Table pipeline_checkpoints is missing. No run data available.")

    row = conn.execute(
        "SELECT context_json FROM pipeline_checkpoints WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if not row:
        raise RuntimeError(f"No checkpoint context found for run_id={run_id}")
    return json.loads(str(row[0]))


def _build_fixture_payload(context: dict, keep_fields: Iterable[str]) -> dict:
    payload = {k: None for k in TOP_LEVEL_KEYS}
    for key in keep_fields:
        payload[key] = context.get(key)
    return payload


def _write_fixtures(context: dict, fixtures_dir: Path, overwrite: bool) -> None:
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    for file_name, keep_fields in FIXTURE_FIELD_MAP.items():
        out_path = fixtures_dir / file_name
        if out_path.exists() and not overwrite:
            print(f"[skip] {out_path} already exists (use --overwrite)")
            continue

        payload = _build_fixture_payload(context, keep_fields)
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

        missing = [field for field in keep_fields if payload.get(field) is None]
        if missing:
            print(f"[wrote] {out_path} (missing fields: {', '.join(missing)})")
        else:
            print(f"[wrote] {out_path}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run real pipeline once and export stage fixture JSON files from checkpoint context"
    )
    parser.add_argument("--job-url", default="", help="Real job URL for the run_real_pipeline manual mode")
    parser.add_argument("--job-platform", default="", help="Optional platform hint (greenhouse, lever, linkedin, ...)" )
    parser.add_argument(
        "--mode",
        choices=["observe", "apply", "apply-dryrun"],
        default="observe",
        help="Mode for run_real_pipeline when not using --skip-run",
    )
    parser.add_argument("--headless", action="store_true", help="Pass --headless to run_real_pipeline")
    parser.add_argument("--skip-run", action="store_true", help="Skip running pipeline and only export from DB")
    parser.add_argument("--run-id", default="", help="Use a specific run_id instead of auto-resolution")
    parser.add_argument("--db-path", default="~/.autorole/pipeline.db", help="SQLite DB path")
    parser.add_argument("--fixtures-dir", default="tests/fixtures", help="Output fixture directory")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing fixture files")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if not args.skip_run and not args.job_url:
        raise SystemExit("--job-url is required unless --skip-run is used")

    if not args.skip_run:
        _run_real_pipeline(args.job_url, args.job_platform, args.mode, args.headless)

    db_path = Path(args.db_path).expanduser()
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        run_id = _resolve_run_id(conn, args.run_id, args.job_url)
        context = _load_checkpoint_context(conn, run_id)
    finally:
        conn.close()

    print(f"[info] using run_id={run_id}")
    _write_fixtures(context, Path(args.fixtures_dir), overwrite=args.overwrite)

    print("[done] fixture export completed")


if __name__ == "__main__":
    main()