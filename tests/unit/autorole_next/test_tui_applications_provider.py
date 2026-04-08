from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from autorole_next._snapflow import RunStatus, StateContext
from autorole_next.payloads import ListingPayload, ListingSeed, SeedRunPayload, canonical_listing_key, correlation_id_for_listing
from autorole_next.stage_ids import FORM_SUBMISSION
from autorole_next.store import AutoRoleStoreAdapter
from autorole_next.tui.applications_provider import SQLiteApplicationsProvider


def _seed() -> ListingSeed:
    return ListingSeed(
        listing=ListingPayload(
            job_url="https://example.com/jobs/job-ctx-1",
            apply_url="https://example.com/jobs/job-ctx-1/apply",
            company_name="Acme",
            external_job_id="job-ctx-1",
            job_title="Platform Engineer",
            platform="workday",
        ),
        source_name="manual-test",
        source_metadata={"fixture": True},
    )


@pytest.mark.asyncio
async def test_applications_provider_returns_context_data_and_artifact_refs(tmp_path) -> None:
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
        current_stage=FORM_SUBMISSION,
        data=payload.model_dump(mode="json"),
        metadata={"source_name": seed.source_name},
    )
    await store.save_context(context)
    await store.create_run(correlation_id, {})
    await store.set_run_status(correlation_id, RunStatus.RUNNING)

    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE pipeline_contexts SET artifact_refs = ? WHERE correlation_id = ?",
            ('[{"kind":"audit","path":"logs/form_submission/audit.json"}]', correlation_id),
        )
        db.commit()

    provider = SQLiteApplicationsProvider(store.path)
    rows = await provider.list_rows()

    assert len(rows) == 1
    assert rows[0].correlation_id == correlation_id
    assert rows[0].run_status == "running"
    assert rows[0].current_stage == FORM_SUBMISSION

    details = await provider.get_details(correlation_id)
    assert details is not None
    assert details["run"]["correlation_id"] == correlation_id
    assert details["context"]["current_stage"] == FORM_SUBMISSION
    assert isinstance(details["context"]["data"], dict)
    artifact_refs = details["context"]["artifact_refs"]
    assert isinstance(artifact_refs, list)
    assert artifact_refs[0]["kind"] == "audit"


@pytest.mark.asyncio
async def test_applications_provider_exports_pipeline_context_payload(tmp_path) -> None:
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
        current_stage=FORM_SUBMISSION,
        data=payload.model_dump(mode="json"),
        metadata={"source_name": seed.source_name, "run_mode": "apply"},
    )
    await store.save_context(context)
    await store.create_run(correlation_id, {})
    await store.set_run_status(correlation_id, RunStatus.RUNNING)

    provider = SQLiteApplicationsProvider(store.path)
    export_dir = tmp_path / "exports"
    export_path = await provider.export_payload(correlation_id, export_dir)

    assert export_path == export_dir / f"{correlation_id}.json"
    exported = json.loads(export_path.read_text(encoding="utf-8"))
    assert exported["run"]["correlation_id"] == correlation_id
    assert exported["context"]["current_stage"] == FORM_SUBMISSION
    assert exported["message_payload"]["correlation_id"] == correlation_id
    assert exported["message_payload"]["current_stage"] == FORM_SUBMISSION
    assert exported["message_payload"]["data"]["listing"]["job_url"] == seed.listing.job_url
    assert exported["message_payload"]["metadata"]["run_mode"] == "apply"


@pytest.mark.asyncio
async def test_applications_provider_detects_pending_form_submission_dlq(tmp_path) -> None:
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
        current_stage=FORM_SUBMISSION,
        data=payload.model_dump(mode="json"),
        metadata={"submit_disabled": True},
    )
    await store.save_context(context)
    await store.create_run(correlation_id, {})
    await store.set_run_status(correlation_id, RunStatus.BLOCKED, reason="manual approval required")

    with sqlite3.connect(store.path) as db:
        db.execute(
            """
            INSERT INTO dlq_messages (
                id,
                queue_name,
                correlation_id,
                stage_name,
                attempt,
                error_category,
                error_message,
                context_snapshot,
                created_at,
                redriven_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "dlq-1",
                "global_dlq",
                correlation_id,
                FORM_SUBMISSION,
                0,
                "business_rule",
                "submission disabled by operator guardrail",
                json.dumps(context.model_dump(mode="json")),
                datetime.now(timezone.utc).isoformat(),
                None,
            ),
        )
        db.commit()

    provider = SQLiteApplicationsProvider(store.path)
    assert await provider.has_pending_form_submission_dlq(correlation_id) is True


@pytest.mark.asyncio
async def test_applications_provider_manual_submit_redrives_to_concluding(tmp_path) -> None:
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
        current_stage=FORM_SUBMISSION,
        data=payload.model_dump(mode="json"),
        metadata={"submit_disabled": True},
    )
    await store.save_context(context)
    await store.create_run(correlation_id, {})
    await store.set_run_status(correlation_id, RunStatus.BLOCKED, reason="manual approval required")

    with sqlite3.connect(store.path) as db:
        db.execute(
            """
            INSERT INTO dlq_messages (
                id,
                queue_name,
                correlation_id,
                stage_name,
                attempt,
                error_category,
                error_message,
                context_snapshot,
                created_at,
                redriven_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "dlq-2",
                "global_dlq",
                correlation_id,
                FORM_SUBMISSION,
                0,
                "business_rule",
                "submission disabled by operator guardrail",
                json.dumps(context.model_dump(mode="json")),
                datetime.now(timezone.utc).isoformat(),
                None,
            ),
        )
        db.commit()

    provider = SQLiteApplicationsProvider(store.path)
    success, message = await provider.manual_submit_to_concluding(correlation_id)

    assert success is True
    assert "concluding" in message.lower()
    assert await provider.has_pending_form_submission_dlq(correlation_id) is False

    with sqlite3.connect(store.path) as db:
        queue_row = db.execute(
            "SELECT queue_name, correlation_id FROM queue_messages ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert queue_row == ("concluding", correlation_id)

        context_row = db.execute(
            "SELECT current_stage FROM pipeline_contexts WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        assert context_row == ("concluding",)

        run_row = db.execute(
            "SELECT status FROM pipeline_runs WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        assert run_row == ("running",)
