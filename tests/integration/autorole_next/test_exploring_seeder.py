from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

import pytest

from autorole_next.app import build_runner, build_store
from autorole_next._snapflow import RunStatus
from autorole_next.payloads import ExplorationInput, ListingPayload, ListingSeed
from autorole_next.seeders.exploring import ExploringSeeder


def _seed(job_id: str = "job-1", source_name: str = "search") -> ListingSeed:
    return ListingSeed(
        listing=ListingPayload(
            job_url=f"https://example.com/jobs/{job_id}",
            apply_url=f"https://example.com/jobs/{job_id}/apply",
            company_name="Acme",
            external_job_id=job_id,
            job_title="Platform Engineer",
            platform="workday",
        ),
        source_name=source_name,
        discovered_at=datetime.now(timezone.utc),
        source_metadata={"job_id": job_id},
    )


@pytest.mark.asyncio
async def test_exploring_seeder_is_idempotent_for_duplicate_search_results(tmp_path) -> None:
    db_path = str(tmp_path / "autorole-next.db")
    runner = build_runner(db_path)
    store = build_store(db_path)

    async def discover(_filters: dict[str, object]) -> list[ListingSeed]:
        return [_seed(), _seed()]

    seeder = ExploringSeeder(runner, store, search_discovery=discover)
    seeded_runs = await seeder.seed(ExplorationInput(search_filters={"platforms": ["mock"]}))

    assert [run.status for run in seeded_runs] == ["seeded"]
    with sqlite3.connect(store.path) as db:
        queued = db.execute("SELECT COUNT(*) FROM queue_messages WHERE queue_name = 'scoring'").fetchone()
    assert queued is not None and int(queued[0]) == 1
    status = await store.get_run(seeded_runs[0].correlation_id)
    assert status is not None
    assert status.status == RunStatus.RUNNING


@pytest.mark.asyncio
async def test_exploring_seeder_supports_url_list_stage(tmp_path) -> None:
	db_path = str(tmp_path / "autorole-next.db")
	runner = build_runner(db_path)
	store = build_store(db_path)

	async def resolve(job_url: str, platform_hint: str | None) -> ListingSeed:
		_ = platform_hint
		return ListingSeed(
			listing=ListingPayload(
				job_url=job_url,
				apply_url=f"{job_url}/apply",
				company_name="Acme",
				external_job_id="job-2",
				job_title="Platform Engineer",
				platform="workday",
			),
			source_name="url-list",
			discovered_at=datetime.now(timezone.utc),
			source_metadata={"mode": "url-list"},
		)

	seeder = ExploringSeeder(runner, store, job_url_resolver=resolve)
	job_urls_file = tmp_path / "job_urls.json"
	job_urls_file.write_text('["https://example.com/jobs/job-2"]', encoding="utf-8")

	seeded_runs = await seeder.seed(ExplorationInput(job_urls_file=str(job_urls_file)))

	assert len(seeded_runs) == 1
	assert seeded_runs[0].status == "seeded"