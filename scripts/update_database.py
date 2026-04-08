"""Update pipeline run status and context stage for rows matching current stage.

Examples:
    python scripts/update_database.py --db tmp/manual-seeder.db --where-stage fieldCompleter --set-status running --set-stage formScraper --apply
    python scripts/update_database.py --db tmp/manual-seeder.db --where-stage formSubmission --set-status running --set-stage formScraper --apply
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

DEFAULT_DB_PATH = "tmp/autorole-next.db"


def _validate_required_tables(connection: sqlite3.Connection) -> None:
    required = {"pipeline_runs", "pipeline_contexts"}
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('pipeline_runs', 'pipeline_contexts')"
    ).fetchall()
    present = {str(row[0]) for row in rows}
    missing = sorted(required - present)
    if missing:
        raise SystemExit(f"Missing required table(s): {', '.join(missing)}")


def _matching_correlation_ids(connection: sqlite3.Connection, where_stage: str) -> list[str]:
    rows = connection.execute(
        "SELECT correlation_id FROM pipeline_contexts WHERE current_stage = ? ORDER BY correlation_id",
        (where_stage,),
    ).fetchall()
    return [str(row[0]) for row in rows]


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row[1]) for row in rows}


def _has_table(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _in_clause_params(values: list[str]) -> tuple[str, list[str]]:
    placeholders = ", ".join("?" for _ in values)
    return f"({placeholders})", list(values)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Update pipeline_runs.status and pipeline_contexts.current_stage in one transaction"
    )
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="Path to SQLite DB file")
    parser.add_argument("--where-stage", required=True, help="Filter rows by pipeline_contexts.current_stage")
    parser.add_argument("--set-status", required=True, help="New value for pipeline_runs.status")
    parser.add_argument("--set-stage", required=True, help="New value for pipeline_contexts.current_stage")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply updates. Without this flag, script runs in dry-run mode.",
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        raise SystemExit(f"Database does not exist: {db_path}")

    connection = sqlite3.connect(db_path)
    try:
        _validate_required_tables(connection)

        correlation_ids = _matching_correlation_ids(connection, args.where_stage)
        print(f"Matched correlation_ids: {len(correlation_ids)}")
        for correlation_id in correlation_ids[:20]:
            print(f"- {correlation_id}")
        if len(correlation_ids) > 20:
            print(f"... and {len(correlation_ids) - 20} more")

        if not correlation_ids:
            print("No rows matched; nothing to update.")
            return

        if not args.apply:
            print("Dry run mode. Re-run with --apply to persist updates.")
            return

        in_clause, in_params = _in_clause_params(correlation_ids)

        queue_result = None
        queue_mode = "none"
        with connection:
            run_result = connection.execute(
                """
                UPDATE pipeline_runs
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE correlation_id IN (
                    SELECT correlation_id
                    FROM pipeline_contexts
                    WHERE current_stage = ?
                )
                """,
                (args.set_status, args.where_stage),
            )
            ctx_result = connection.execute(
                """
                UPDATE pipeline_contexts
                SET current_stage = ?, updated_at = CURRENT_TIMESTAMP
                WHERE current_stage = ?
                """,
                (args.set_stage, args.where_stage),
            )

            if _has_table(connection, "queue_messages"):
                queue_columns = _table_columns(connection, "queue_messages")
                if {"queue_name", "correlation_id", "visible_at"}.issubset(queue_columns):
                    queue_mode = "queue_name"
                    queue_result = connection.execute(
                        f"""
                        UPDATE queue_messages
                        SET queue_name = ?, visible_at = 0, locked_until = NULL
                        WHERE correlation_id IN {in_clause}
                        """,
                        [args.set_stage, *in_params],
                    )
                elif {"stage_name", "correlation_id", "status"}.issubset(queue_columns):
                    queue_mode = "stage_name"
                    queue_result = connection.execute(
                        f"""
                        UPDATE queue_messages
                        SET stage_name = ?, status = 'queued', visible_after = NULL
                        WHERE correlation_id IN {in_clause}
                        """,
                        [args.set_stage, *in_params],
                    )

        print(f"Updated pipeline_runs rows: {run_result.rowcount}")
        print(f"Updated pipeline_contexts rows: {ctx_result.rowcount}")
        if queue_result is not None:
            print(f"Updated queue_messages rows ({queue_mode}): {queue_result.rowcount}")
        else:
            print("Updated queue_messages rows: skipped (table/columns not recognized)")
    finally:
        connection.close()


if __name__ == "__main__":
    main()
