from __future__ import annotations

import pytest

from autorole_next._snapflow import RunStatus, StateContext
from autorole_next.payloads import ListingPayload, ListingSeed, SeedRunPayload, canonical_listing_key, correlation_id_for_listing
from autorole_next.stage_ids import SCORING
from autorole_next.store import AutoRoleStoreAdapter
from autorole_next.tui.listings_provider import SQLiteListingsProvider


def _seed() -> ListingSeed:
    return ListingSeed(
        listing=ListingPayload(
            job_url="https://example.com/jobs/job-1",
            apply_url="https://example.com/jobs/job-1/apply",
            company_name="Acme",
            external_job_id="job-1",
            job_title="Platform Engineer",
            platform="workday",
        ),
        source_name="manual-test",
        source_metadata={"fixture": True},
    )


@pytest.mark.asyncio
async def test_listings_provider_returns_runtime_joined_rows(tmp_path) -> None:
    store = AutoRoleStoreAdapter(str(tmp_path / "autorole-next.db"))
    seed = _seed()
    correlation_id = correlation_id_for_listing(seed.listing)
    canonical_key = canonical_listing_key(seed.listing)

    created, _ = await store.claim_listing_seed(correlation_id, seed)
    assert created is True

    payload = SeedRunPayload(
        listing=seed.listing,
        source_name=seed.source_name,
        source_metadata=seed.source_metadata,
        discovered_at=seed.discovered_at,
        canonical_key=canonical_key,
    )
    context = StateContext[dict[str, object]](
        correlation_id=correlation_id,
        current_stage=SCORING,
        data=payload.model_dump(mode="json"),
        metadata={"source_name": seed.source_name},
    )
    await store.save_context(context)
    await store.create_run(correlation_id, {})
    await store.set_run_status(correlation_id, RunStatus.RUNNING)

    provider = SQLiteListingsProvider(store.path)
    rows = await provider.list_rows()

    assert len(rows) == 1
    assert rows[0].company_name == "Acme"
    assert rows[0].job_title == "Platform Engineer"
    assert rows[0].run_status == "running"
    assert rows[0].current_stage == SCORING

    details = await provider.get_details(correlation_id)

    assert details is not None
    assert details["listing"]["correlation_id"] == correlation_id
    assert details["runtime"]["run_status"] == "running"
    assert details["runtime"]["current_stage"] == SCORING