from __future__ import annotations

import sqlite3

import pytest

from autorole_next._snapflow import RunStatus
from autorole_next.payloads import ListingPayload, ListingSeed
from autorole_next.store import AutoRoleStoreAdapter


def _seed(job_id: str = "job-1") -> ListingSeed:
    return ListingSeed(
        listing=ListingPayload(
            job_url=f"https://example.com/jobs/{job_id}",
            apply_url=f"https://example.com/jobs/{job_id}/apply",
            company_name="Acme",
            external_job_id=job_id,
            job_title="Platform Engineer",
            platform="workday",
        ),
        source_name="test-source",
        source_metadata={"fixture": True},
    )


@pytest.mark.asyncio
async def test_store_claim_listing_seed_is_idempotent(tmp_path) -> None:
    store = AutoRoleStoreAdapter(str(tmp_path / "autorole-next.db"))
    seed = _seed()

    created, first_row = await store.claim_listing_seed("corr-1", seed)
    created_again, second_row = await store.claim_listing_seed("corr-2", seed)

    assert created is True
    assert created_again is False
    assert first_row["canonical_key"] == second_row["canonical_key"]
    assert second_row["correlation_id"] == "corr-1"


@pytest.mark.asyncio
async def test_store_runtime_tables_use_correlation_id_and_include_queue_state(tmp_path) -> None:
    store = AutoRoleStoreAdapter(str(tmp_path / "autorole-next.db"))
    await store.create_run("corr-1", {})
    await store.set_run_status("corr-1", RunStatus.RUNNING)

    with sqlite3.connect(store.path) as db:
        expected_columns = {
            "pipeline_runs": {"correlation_id", "status", "reason", "started_at", "updated_at"},
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
        for table_name, required in expected_columns.items():
            columns = {str(row[1]) for row in db.execute(f"PRAGMA table_info({table_name})").fetchall()}
            assert required.issubset(columns)
            assert "run_id" not in columns


@pytest.mark.asyncio
async def test_store_integrity_report_flags_missing_runtime_tables(tmp_path) -> None:
    db_path = tmp_path / "autorole-next.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TABLE pipeline_runs (correlation_id TEXT PRIMARY KEY, status TEXT, reason TEXT, started_at TEXT, updated_at TEXT)"
        )
        db.commit()

    store = AutoRoleStoreAdapter(str(db_path))
    report = await store.integrity_report()

    assert "pipeline_contexts" not in report["runtime_tables"]
    assert report["runtime_tables"] == []
    assert report["runtime_columns"] == []
    assert report["domain_tables"] == []
