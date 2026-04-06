from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from autorole_next._snapflow import RunStatus
from autorole_next.app import build_runner, build_store
from autorole_next.payloads import ExplorationInput, ListingPayload, ListingSeed
from autorole_next.seeders.exploring import ExploringSeeder


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
async def test_scoring_slice_completes_on_high_score(tmp_path) -> None:
    db_path = str(tmp_path / "autorole-next.db")
    runner = build_runner(db_path)
    store = build_store(db_path)

    async def discover(_filters: dict[str, object]) -> list[ListingSeed]:
        return [_seed("pass-case")]

    seeder = ExploringSeeder(runner, store, search_discovery=discover)
    await runner.start(stage_ids=["scoring", "tailoring", "packaging", "session", "formScraper", "fieldCompleter", "formSubmission", "concluding"])
    try:
        seeded = await seeder.seed(ExplorationInput(search_filters={"platforms": ["mock"]}, metadata={"forced_score": 0.93}))
        status = await _wait_for_terminal_status(store, seeded[0].correlation_id)
        assert status == RunStatus.COMPLETED

        with sqlite3.connect(store.path) as db:
            attempts = db.execute(
                "SELECT attempt, overall_score FROM score_reports WHERE correlation_id = ? ORDER BY attempt ASC",
                (seeded[0].correlation_id,),
            ).fetchall()

        assert len(attempts) == 1
        assert attempts[0][0] == 1
        assert float(attempts[0][1]) >= 0.9
    finally:
        await runner.shutdown(mode="hard")


@pytest.mark.asyncio
async def test_scoring_slice_blocks_after_loop_attempts(tmp_path) -> None:
    db_path = str(tmp_path / "autorole-next.db")
    runner = build_runner(db_path)
    store = build_store(db_path)

    async def discover(_filters: dict[str, object]) -> list[ListingSeed]:
        return [_seed("block-case")]

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
            attempts = db.execute(
                "SELECT attempt, overall_score FROM score_reports WHERE correlation_id = ? ORDER BY attempt ASC",
                (seeded[0].correlation_id,),
            ).fetchall()
            tailoring_attempts = db.execute(
                "SELECT attempt FROM tailored_resumes WHERE correlation_id = ? ORDER BY attempt ASC",
                (seeded[0].correlation_id,),
            ).fetchall()

        assert [row[0] for row in attempts] == [1, 2, 3]
        assert [row[0] for row in tailoring_attempts] == [1, 2, 3]
    finally:
        await runner.shutdown(mode="hard")