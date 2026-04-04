from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from autorole_next._snapflow import RunStatus
from autorole_next.app import build_runner, build_store
from autorole_next.payloads import ExplorationInput, ListingPayload, ListingSeed
from autorole_next.seeders.exploring import ExploringSeeder


def _seed(job_id: str = "form-scraper-1") -> ListingSeed:
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
async def test_form_scraper_slice_populates_form_payload_and_completes(tmp_path) -> None:
    db_path = str(tmp_path / "autorole-next.db")
    runner = build_runner(db_path)
    store = build_store(db_path)

    async def discover(_filters: dict[str, object]) -> list[ListingSeed]:
        return [_seed("form-scraper")]

    seeder = ExploringSeeder(runner, store, search_discovery=discover)
    await runner.start(stage_ids=["scoring", "tailoring", "packaging", "session", "formScraper", "fieldCompleter", "formSubmission", "concluding"])
    try:
        seeded = await seeder.seed(
            ExplorationInput(
                search_filters={"platforms": ["mock"]},
                metadata={"forced_score": 0.93},
            )
        )

        correlation_id = seeded[0].correlation_id
        status = await _wait_for_terminal_status(store, correlation_id)
        assert status == RunStatus.COMPLETED

        ctx = await store.load_context(correlation_id)
        assert ctx is not None
        data = dict(ctx.data)

        form_payload = data.get("formScraper")
        assert isinstance(form_payload, dict)
        assert form_payload.get("platform") == "workday"
        fields = form_payload.get("extracted_fields")
        assert isinstance(fields, list)
        assert len(fields) >= 3

        # Alias kept for migration compatibility.
        assert data.get("form_intelligence") == form_payload
    finally:
        await runner.shutdown(mode="hard")
