from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite


@dataclass(frozen=True)
class ApplicationSummary:
    correlation_id: str
    run_status: str
    current_stage: str
    attempt: int
    updated_at: str


class SQLiteApplicationsProvider:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(Path(db_path))

    async def list_rows(self, search: str = "", limit: int = 200, offset: int = 0) -> list[ApplicationSummary]:
        text = search.strip().lower()
        rows: list[ApplicationSummary] = []

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.execute(
                """
                SELECT
                    pc.correlation_id,
                    COALESCE(pr.status, ''),
                    COALESCE(pc.current_stage, ''),
                    COALESCE(pc.attempt, 0),
                    COALESCE(pc.updated_at, '')
                FROM pipeline_contexts AS pc
                LEFT JOIN pipeline_runs AS pr ON pr.correlation_id = pc.correlation_id
                ORDER BY pc.updated_at DESC, pc.correlation_id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            result_rows = await cursor.fetchall()

        for row in result_rows:
            summary = ApplicationSummary(
                correlation_id=str(row[0]),
                run_status=str(row[1]),
                current_stage=str(row[2]),
                attempt=int(row[3] or 0),
                updated_at=str(row[4]),
            )
            if self._matches_search(summary, text):
                rows.append(summary)

        return rows

    async def get_details(self, correlation_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.execute(
                """
                SELECT
                    pc.correlation_id,
                    COALESCE(pr.status, ''),
                    COALESCE(pr.reason, ''),
                    COALESCE(pr.updated_at, ''),
                    COALESCE(pc.trace_id, ''),
                    COALESCE(pc.current_stage, ''),
                    COALESCE(pc.attempt, 0),
                    COALESCE(pc.data, '{}'),
                    COALESCE(pc.artifact_refs, '[]'),
                    COALESCE(pc.metadata, '{}'),
                    COALESCE(pc.created_at, ''),
                    COALESCE(pc.updated_at, '')
                FROM pipeline_contexts AS pc
                LEFT JOIN pipeline_runs AS pr ON pr.correlation_id = pc.correlation_id
                WHERE pc.correlation_id = ?
                """,
                (correlation_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None

        return {
            "run": {
                "correlation_id": row[0],
                "status": row[1],
                "reason": row[2],
                "updated_at": row[3],
            },
            "context": {
                "trace_id": row[4],
                "current_stage": row[5],
                "attempt": int(row[6] or 0),
                "data": self._decode_json(str(row[7] or "{}"), fallback_key="raw_data"),
                "artifact_refs": self._decode_json(str(row[8] or "[]"), fallback_key="raw_artifact_refs"),
                "metadata": self._decode_json(str(row[9] or "{}"), fallback_key="raw_metadata"),
                "created_at": row[10],
                "updated_at": row[11],
            },
        }

    @staticmethod
    def _decode_json(payload: str, *, fallback_key: str) -> Any:
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {fallback_key: payload}

    @staticmethod
    def _matches_search(row: ApplicationSummary, search: str) -> bool:
        if not search:
            return True
        blob = " ".join(
            [
                row.correlation_id,
                row.run_status,
                row.current_stage,
                str(row.attempt),
                row.updated_at,
            ]
        ).lower()
        return search in blob


def build_applications_provider_from_env() -> SQLiteApplicationsProvider:
    db_path = os.getenv("SNAPFLOW_TUI_DB", "pipeline.db")
    return SQLiteApplicationsProvider(db_path)
