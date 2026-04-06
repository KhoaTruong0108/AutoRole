from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import orjson

from ._snapflow import SQLiteStoreAdapter
from .payloads import ListingSeed, canonical_listing_key
from .schema import DOMAIN_SCHEMA_SQL, DOMAIN_TABLES
from .stage_ids import STAGE_ALIASES

RUNTIME_REQUIRED_TABLES = {
    "pipeline_runs",
    "pipeline_contexts",
    "queue_messages",
    "dlq_messages",
}

RUNTIME_REQUIRED_COLUMNS = {
    "pipeline_runs": {
        "correlation_id",
        "status",
        "reason",
        "started_at",
        "updated_at",
    },
    "pipeline_contexts": {
        "correlation_id",
        "trace_id",
        "current_stage",
        "attempt",
        "data",
        "artifact_refs",
        "metadata",
        "created_at",
        "updated_at",
    },
    "queue_messages": {
        "id",
        "queue_name",
        "correlation_id",
        "visible_at",
        "locked_until",
        "delivery_count",
        "created_at",
    },
    "dlq_messages": {
        "id",
        "queue_name",
        "correlation_id",
        "stage_name",
        "attempt",
        "error_category",
        "error_message",
        "context_snapshot",
        "created_at",
        "redriven_at",
    },
}


class AutoRoleStoreAdapter(SQLiteStoreAdapter):
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._domain_initialized = False
        self._domain_init_lock = asyncio.Lock()
        super().__init__(str(self._path))

    @property
    def path(self) -> Path:
        return self._path

    async def claim_listing_seed(
        self,
        correlation_id: str,
        seed: ListingSeed,
    ) -> tuple[bool, dict[str, Any]]:
        await self._ensure_initialized()
        listing = seed.listing
        listing_key = canonical_listing_key(listing)
        payload_json = orjson.dumps(seed.source_metadata).decode("utf-8")
        discovered_at = seed.discovered_at.isoformat()

        aiosqlite = self._import_aiosqlite()
        async with aiosqlite.connect(self._db_path) as connection:
            cursor = await connection.execute(
                """
                INSERT OR IGNORE INTO listings (
                    listing_key,
                    correlation_id,
                    source_name,
                    source_metadata,
                    job_url,
                    apply_url,
                    company_name,
                    job_title,
                    external_job_id,
                    platform,
                    status,
                    discovered_at,
                    first_seen_at,
                    last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'seeded', ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (
                    listing_key,
                    correlation_id,
                    seed.source_name,
                    payload_json,
                    listing.job_url,
                    listing.apply_url,
                    listing.company_name,
                    listing.job_title,
                    listing.external_job_id,
                    listing.platform,
                    discovered_at,
                ),
            )
            created = cursor.rowcount == 1
            if not created:
                await connection.execute(
                    "UPDATE listings SET last_seen_at = CURRENT_TIMESTAMP WHERE listing_key = ?",
                    (listing_key,),
                )
            await connection.commit()

            row_cursor = await connection.execute(
                """
                SELECT
                    listing_key,
                    correlation_id,
                    source_name,
                    job_url,
                    status,
                    discovered_at,
                    first_seen_at,
                    last_seen_at
                FROM listings
                WHERE listing_key = ?
                """,
                (listing_key,),
            )
            row = await row_cursor.fetchone()

        if row is None:
            raise RuntimeError(f"Listing record missing after claim: {listing_key}")
        return created, self._listing_row_to_dict(row)

    async def integrity_report(self) -> dict[str, list[str]]:
        await self._ensure_initialized()
        issues: dict[str, list[str]] = {
            "runtime_tables": [],
            "runtime_columns": [],
            "domain_tables": [],
        }
        with sqlite3.connect(self._db_path) as connection:
            issues["runtime_tables"] = self._query_issues(connection, RUNTIME_REQUIRED_TABLES)
            issues["runtime_columns"] = self._validate_runtime_schema(connection)
            issues["domain_tables"] = self._query_issues(connection, DOMAIN_TABLES)
        return issues

    async def append_score_report(
        self,
        correlation_id: str,
        *,
        attempt: int,
        overall_score: float,
        criteria_scores: dict[str, Any],
        matched: list[str],
        mismatched: list[str],
        jd_summary: str,
    ) -> None:
        await self._ensure_initialized()
        aiosqlite = self._import_aiosqlite()
        async with aiosqlite.connect(self._db_path) as connection:
            await connection.execute(
                """
                INSERT INTO score_reports (
                    correlation_id,
                    attempt,
                    overall_score,
                    criteria_json,
                    matched_json,
                    mismatched_json,
                    jd_summary,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    correlation_id,
                    attempt,
                    float(overall_score),
                    orjson.dumps(criteria_scores).decode("utf-8"),
                    orjson.dumps(matched).decode("utf-8"),
                    orjson.dumps(mismatched).decode("utf-8"),
                    jd_summary,
                ),
            )
            await connection.commit()

    async def append_tailored_resume(
        self,
        correlation_id: str,
        *,
        attempt: int,
        resume_path: str,
        diff_summary: str,
        tailoring_degree: int,
    ) -> None:
        await self._ensure_initialized()
        aiosqlite = self._import_aiosqlite()
        async with aiosqlite.connect(self._db_path) as connection:
            await connection.execute(
                """
                INSERT INTO tailored_resumes (
                    correlation_id,
                    attempt,
                    resume_path,
                    diff_summary,
                    tailoring_degree,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    correlation_id,
                    int(attempt),
                    resume_path,
                    diff_summary,
                    int(tailoring_degree),
                ),
            )
            await connection.commit()

    async def upsert_application_packaging(
        self,
        correlation_id: str,
        *,
        resume_path: str,
        pdf_path: str,
    ) -> None:
        await self._ensure_initialized()
        aiosqlite = self._import_aiosqlite()
        async with aiosqlite.connect(self._db_path) as connection:
            await connection.execute(
                """
                INSERT INTO applications (
                    correlation_id,
                    status,
                    resume_path,
                    pdf_path,
                    created_at,
                    updated_at
                ) VALUES (?, 'packaged', ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(correlation_id) DO UPDATE SET
                    status = 'packaged',
                    resume_path = excluded.resume_path,
                    pdf_path = excluded.pdf_path,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    correlation_id,
                    resume_path,
                    pdf_path,
                ),
            )
            await connection.commit()

    async def upsert_session(
        self,
        correlation_id: str,
        *,
        platform: str,
        authenticated: bool,
        session_note: str,
    ) -> None:
        await self._ensure_initialized()
        aiosqlite = self._import_aiosqlite()
        async with aiosqlite.connect(self._db_path) as connection:
            await connection.execute(
                """
                INSERT INTO sessions (
                    correlation_id,
                    platform,
                    authenticated,
                    session_note,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(correlation_id) DO UPDATE SET
                    platform = excluded.platform,
                    authenticated = excluded.authenticated,
                    session_note = excluded.session_note,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    correlation_id,
                    platform,
                    int(authenticated),
                    session_note,
                ),
            )
            await connection.commit()

    async def upsert_application_status(
        self,
        correlation_id: str,
        *,
        status: str,
    ) -> None:
        await self._ensure_initialized()
        aiosqlite = self._import_aiosqlite()
        async with aiosqlite.connect(self._db_path) as connection:
            await connection.execute(
                """
                INSERT INTO applications (
                    correlation_id,
                    status,
                    created_at,
                    updated_at
                ) VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(correlation_id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    correlation_id,
                    status,
                ),
            )
            await connection.commit()

    async def upsert_application_submission(
        self,
        correlation_id: str,
        *,
        status: str,
        confirmed: bool,
        applied_at: str,
    ) -> None:
        await self._ensure_initialized()
        aiosqlite = self._import_aiosqlite()
        async with aiosqlite.connect(self._db_path) as connection:
            await connection.execute(
                """
                INSERT INTO applications (
                    correlation_id,
                    status,
                    confirmed,
                    applied_at,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(correlation_id) DO UPDATE SET
                    status = excluded.status,
                    confirmed = excluded.confirmed,
                    applied_at = excluded.applied_at,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    correlation_id,
                    status,
                    int(confirmed),
                    applied_at,
                ),
            )
            await connection.commit()

    async def finalize_application_projection(
        self,
        correlation_id: str,
        *,
        final_score: float,
        resume_path: str,
        pdf_path: str,
    ) -> None:
        await self._ensure_initialized()
        aiosqlite = self._import_aiosqlite()
        async with aiosqlite.connect(self._db_path) as connection:
            await connection.execute(
                """
                INSERT INTO applications (
                    correlation_id,
                    final_score,
                    resume_path,
                    pdf_path,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(correlation_id) DO UPDATE SET
                    final_score = excluded.final_score,
                    resume_path = CASE
                        WHEN excluded.resume_path = '' THEN applications.resume_path
                        ELSE excluded.resume_path
                    END,
                    pdf_path = CASE
                        WHEN excluded.pdf_path = '' THEN applications.pdf_path
                        ELSE excluded.pdf_path
                    END,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    correlation_id,
                    float(final_score),
                    resume_path,
                    pdf_path,
                ),
            )
            await connection.commit()

    async def _ensure_initialized(self) -> None:
        await super()._ensure_initialized()
        if self._domain_initialized:
            return

        async with self._domain_init_lock:
            if self._domain_initialized:
                return

            aiosqlite = self._import_aiosqlite()
            async with aiosqlite.connect(self._db_path) as connection:
                await connection.executescript(DOMAIN_SCHEMA_SQL)
                await self._normalize_stage_names(connection)
                await connection.commit()

            self._domain_initialized = True

    async def _normalize_stage_names(self, connection: Any) -> None:
        for alias, canonical in STAGE_ALIASES.items():
            await connection.execute(
                "UPDATE pipeline_contexts SET current_stage = ? WHERE current_stage = ?",
                (canonical, alias),
            )
            await connection.execute(
                "UPDATE queue_messages SET queue_name = ? WHERE queue_name = ?",
                (canonical, alias),
            )
            await connection.execute(
                "UPDATE dlq_messages SET stage_name = ? WHERE stage_name = ?",
                (canonical, alias),
            )

    def _validate_runtime_schema(self, connection: sqlite3.Connection) -> list[str]:
        issues: list[str] = []
        for table_name, required_columns in RUNTIME_REQUIRED_COLUMNS.items():
            existing = {
                str(row[1])
                for row in connection.execute(f"PRAGMA table_info({table_name})")
            }
            missing = sorted(required_columns - existing)
            for column_name in missing:
                issues.append(f"{table_name}.{column_name}")
        return issues

    def _query_issues(
        self,
        connection: sqlite3.Connection,
        expected_tables: Iterable[str],
    ) -> list[str]:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        actual_tables = {str(row[0]) for row in rows}
        return sorted(set(expected_tables) - actual_tables)

    @staticmethod
    def _listing_row_to_dict(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
        return {
            "canonical_key": row[0],
            "correlation_id": row[1],
            "source_name": row[2],
            "job_url": row[3],
            "status": row[4],
            "discovered_at": row[5],
            "first_seen_at": row[6],
            "last_seen_at": row[7],
        }
