from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite


@dataclass(frozen=True)
class ListingSummary:
    correlation_id: str
    company_name: str
    job_title: str
    platform: str
    source_name: str
    listing_status: str
    run_status: str
    current_stage: str
    updated_at: str
    job_url: str


class SQLiteListingsProvider:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(Path(db_path))

    async def list_rows(self, search: str = "", limit: int = 200, offset: int = 0) -> list[ListingSummary]:
        text = search.strip().lower()
        rows: list[ListingSummary] = []

        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.execute(
                """
                SELECT
                    l.correlation_id,
                    l.company_name,
                    l.job_title,
                    l.platform,
                    l.source_name,
                    l.status,
                    COALESCE(pr.status, ''),
                    COALESCE(pc.current_stage, ''),
                    l.last_seen_at,
                    l.job_url
                FROM listings AS l
                LEFT JOIN pipeline_runs AS pr ON pr.correlation_id = l.correlation_id
                LEFT JOIN pipeline_contexts AS pc ON pc.correlation_id = l.correlation_id
                ORDER BY l.last_seen_at DESC, l.correlation_id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            )
            result_rows = await cursor.fetchall()

        for row in result_rows:
            summary = ListingSummary(
                correlation_id=str(row[0]),
                company_name=str(row[1]),
                job_title=str(row[2]),
                platform=str(row[3]),
                source_name=str(row[4]),
                listing_status=str(row[5]),
                run_status=str(row[6]),
                current_stage=str(row[7]),
                updated_at=str(row[8]),
                job_url=str(row[9]),
            )
            if self._matches_search(summary, text):
                rows.append(summary)

        return rows

    async def get_details(self, correlation_id: str) -> dict[str, Any] | None:
        async with aiosqlite.connect(self._db_path) as conn:
            cursor = await conn.execute(
                """
                SELECT
                    l.listing_key,
                    l.correlation_id,
                    l.source_name,
                    l.source_metadata,
                    l.job_url,
                    l.apply_url,
                    l.company_name,
                    l.job_title,
                    l.external_job_id,
                    l.platform,
                    l.status,
                    l.discovered_at,
                    l.first_seen_at,
                    l.last_seen_at,
                    COALESCE(pr.status, ''),
                    COALESCE(pr.reason, ''),
                    COALESCE(pr.updated_at, ''),
                    COALESCE(pc.current_stage, ''),
                    COALESCE(pc.attempt, 0),
                    COALESCE(pc.data, '{}')
                FROM listings AS l
                LEFT JOIN pipeline_runs AS pr ON pr.correlation_id = l.correlation_id
                LEFT JOIN pipeline_contexts AS pc ON pc.correlation_id = l.correlation_id
                WHERE l.correlation_id = ?
                """,
                (correlation_id,),
            )
            row = await cursor.fetchone()

        if row is None:
            return None

        try:
            source_metadata = json.loads(str(row[3] or "{}"))
        except json.JSONDecodeError:
            source_metadata = {"raw": str(row[3])}

        try:
            context_data = json.loads(str(row[19] or "{}"))
        except json.JSONDecodeError:
            context_data = {"raw": str(row[19])}

        return {
            "listing": {
                "listing_key": row[0],
                "correlation_id": row[1],
                "source_name": row[2],
                "source_metadata": source_metadata,
                "job_url": row[4],
                "apply_url": row[5],
                "company_name": row[6],
                "job_title": row[7],
                "external_job_id": row[8],
                "platform": row[9],
                "status": row[10],
                "discovered_at": row[11],
                "first_seen_at": row[12],
                "last_seen_at": row[13],
            },
            "runtime": {
                "run_status": row[14],
                "reason": row[15],
                "run_updated_at": row[16],
                "current_stage": row[17],
                "attempt": row[18],
                "context_data": context_data,
            },
        }

    @staticmethod
    def _matches_search(row: ListingSummary, search: str) -> bool:
        if not search:
            return True
        blob = " ".join(
            [
                row.correlation_id,
                row.company_name,
                row.job_title,
                row.platform,
                row.source_name,
                row.listing_status,
                row.run_status,
                row.current_stage,
                row.job_url,
            ]
        ).lower()
        return search in blob


def build_listings_provider_from_env() -> SQLiteListingsProvider:
    db_path = os.getenv("SNAPFLOW_TUI_DB", "pipeline.db")
    return SQLiteListingsProvider(db_path)