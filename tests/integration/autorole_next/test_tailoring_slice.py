from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from autorole_next._snapflow import RunStatus
from autorole_next.app import build_runner, build_store
from autorole_next.payloads import ExplorationInput, ListingPayload, ListingSeed
from autorole_next.seeders.exploring import ExploringSeeder


def _seed(job_id: str = "tailor-1") -> ListingSeed:
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
async def test_tailoring_slice_persists_tailored_resumes_for_looping_run(tmp_path) -> None:
    db_path = str(tmp_path / "autorole-next.db")
    runner = build_runner(db_path)
    store = build_store(db_path)

    async def discover(_filters: dict[str, object]) -> list[ListingSeed]:
        return [_seed("tailoring-loop")]

    seeder = ExploringSeeder(runner, store, search_discovery=discover)
    await runner.start(stage_ids=["scoring", "tailoring", "packaging", "session", "formScraper", "fieldCompleter", "formSubmission", "concluding"])
    try:
        seeded = await seeder.seed(
            ExplorationInput(
                search_filters={"platforms": ["mock"]},
                metadata={"forced_score": 0.35, "tailoring_use_llm": False},
            )
        )

        status = await _wait_for_terminal_status(store, seeded[0].correlation_id)
        assert status == RunStatus.BLOCKED

        with sqlite3.connect(store.path) as db:
            rows = db.execute(
                "SELECT attempt, resume_path, tailoring_degree FROM tailored_resumes WHERE correlation_id = ? ORDER BY attempt ASC",
                (seeded[0].correlation_id,),
            ).fetchall()

        assert [row[0] for row in rows] == [1, 2, 3]
        assert all(str(row[1]).endswith(".md") for row in rows)
        assert all(int(row[2]) >= 1 for row in rows)
    finally:
        await runner.shutdown(mode="hard")