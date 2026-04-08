from __future__ import annotations

import json
import os
from datetime import datetime, timezone
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

    async def export_payload(self, correlation_id: str, destination_dir: str | Path | None = None) -> Path | None:
        details = await self.get_details(correlation_id)
        if details is None:
            return None

        export_dir = Path(destination_dir) if destination_dir is not None else Path("logs") / "tui_exports" / "applications"
        export_dir.mkdir(parents=True, exist_ok=True)

        export_path = export_dir / f"{correlation_id}.json"
        export_payload = self._build_export_payload(details)
        export_path.write_text(json.dumps(export_payload, indent=2) + "\n", encoding="utf-8")
        return export_path

    @staticmethod
    def _decode_json(payload: str, *, fallback_key: str) -> Any:
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return {fallback_key: payload}

    @staticmethod
    def _build_export_payload(details: dict[str, Any]) -> dict[str, Any]:
        run_payload = details.get("run") if isinstance(details.get("run"), dict) else {}
        context_payload = details.get("context") if isinstance(details.get("context"), dict) else {}
        return {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "run": run_payload,
            "context": context_payload,
            "message_payload": {
                "correlation_id": str(run_payload.get("correlation_id") or ""),
                "trace_id": str(context_payload.get("trace_id") or ""),
                "current_stage": str(context_payload.get("current_stage") or ""),
                "attempt": int(context_payload.get("attempt") or 0),
                "data": context_payload.get("data"),
                "artifact_refs": context_payload.get("artifact_refs"),
                "metadata": context_payload.get("metadata"),
            },
        }

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
