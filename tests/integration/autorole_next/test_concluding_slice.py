from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from autorole_next._snapflow import RunStatus
from autorole_next.app import build_runner, build_store
from autorole_next.payloads import ExplorationInput, ListingPayload, ListingSeed
from autorole_next.seeders.exploring import ExploringSeeder


def _seed(job_id: str = "concluding-1") -> ListingSeed:
    return ListingSeed(
        listing=ListingPayload(
            job_url=f"https://example.com/jobs/{job_id}",
            apply_url=f"https://example.com/jobs/{job_id}/apply",
            company_name="Acme",
            external_job_id=job_id,
            job_title="Platform Engineer",
            platform="workday",
        ),
        source_name="search",
        discovered_at=datetime.now(timezone.utc),
        source_metadata={"job_id": job_id},
    )


async def _wait_for_terminal_status(store, correlation_id: str, timeout_seconds: float = 5.0) -> RunStatus:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        run = await store.get_run(correlation_id)
        if run is not None and run.status in {RunStatus.COMPLETED, RunStatus.BLOCKED, RunStatus.ERROR}:
            return run.status
        await asyncio.sleep(0.05)
    raise TimeoutError(f"run {correlation_id} did not reach terminal status")


@pytest.mark.asyncio
async def test_concluding_slice_finalizes_application_projection_and_artifact(tmp_path) -> None:
    db_path = str(tmp_path / "autorole-next.db")
    runner = build_runner(db_path)
    store = build_store(db_path)

    async def discover(_filters: dict[str, object]) -> list[ListingSeed]:
        return [_seed("concluding")]

    seeder = ExploringSeeder(runner, store, search_discovery=discover)
    await runner.start(stage_ids=["scoring", "tailoring", "packaging", "session", "formScraper", "fieldCompleter", "formSubmission", "concluding"])
    try:
        seeded = await seeder.seed(
            ExplorationInput(
                search_filters={"platforms": ["mock"]},
                metadata={"forced_score": 0.93, "apply_mode": "dry_run"},
            )
        )

        correlation_id = seeded[0].correlation_id
        status = await _wait_for_terminal_status(store, correlation_id)
        assert status == RunStatus.COMPLETED

        ctx = await store.load_context(correlation_id)
        assert ctx is not None
        concluding = dict(ctx.data).get("concluding")
        assert isinstance(concluding, dict)
        assert float(concluding.get("final_score", 0.0)) >= 0.9
        assert str(concluding.get("submission_status", "")) == "dry_run"

        with sqlite3.connect(store.path) as db:
            row = db.execute(
                "SELECT status, final_score, resume_path, pdf_path FROM applications WHERE correlation_id = ?",
                (correlation_id,),
            ).fetchone()

        assert row is not None
        assert row[0] == "dry_run"
        assert float(row[1]) >= 0.9
        assert str(row[2]).endswith(".md")
        assert str(row[3]).endswith(".pdf")

        artifact_path = Path("logs") / "concluding" / correlation_id / "output.json"
        assert artifact_path.exists()
    finally:
        await runner.shutdown(mode="hard")
