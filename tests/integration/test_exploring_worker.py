from __future__ import annotations

import logging
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from autorole.config import AppConfig
from autorole.context import JobApplicationContext
from autorole.context import JobListing
from autorole.queue import DEAD_LETTER_Q, EXPLORING_Q, InMemoryQueueBackend, SCORING_Q
from autorole.stages.exploring import ExploringStage
from autorole.workers.base import WorkerConfig
from autorole.workers.exploring import ExploringWorker
from tests.conftest import MockStage, load_fixture, make_worker_message


class _ExplodingStage:
    async def execute(self, message: object) -> object:
        _ = message
        raise RuntimeError("boom")


def _result(success: bool, output: object = None, error: str = "") -> SimpleNamespace:
    return SimpleNamespace(success=success, output=output, error=error)


class _MockScraper:
    def __init__(self, listings: list[JobListing]) -> None:
        self._listings = listings

    async def search(self, filters: object) -> list[JobListing]:
        _ = filters
        return self._listings


def _listing(company: str, job_id: str, platform: str) -> JobListing:
    return JobListing(
        job_url=f"https://example.com/{platform}/{job_id}",
        apply_url=f"https://example.com/{platform}/{job_id}",
        company_name=company,
        job_id=job_id,
        job_title=f"{company} Engineer {job_id}",
        platform=platform,
        crawled_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_exploring_worker_success(repo, tmp_path):
    output_fixture = load_fixture("qualification_input.json")
    queue = InMemoryQueueBackend()
    config = WorkerConfig(EXPLORING_Q, SCORING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = ExploringWorker(
        stage=MockStage(_result(True, [output_fixture])),
        repo=repo,
        logger=logging.getLogger("test.exploring"),
        artifacts_root=tmp_path,
        config=config,
    )
    seed = {
        "run_id": "seed",
        "job_url": "https://example.com/job/123",
        "max_listings": 1,
    }
    msg = make_worker_message(seed, EXPLORING_Q, SCORING_Q)
    await queue.enqueue(EXPLORING_Q, msg)
    pulled = await queue.pull(EXPLORING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    out = await queue.pull(SCORING_Q)
    assert out is not None
    out_ctx = JobApplicationContext.model_validate(out.payload)
    assert out_ctx.listing is not None
    assert await queue.pull(DEAD_LETTER_Q) is None

    async with repo._db.execute(
        "SELECT COUNT(*) FROM job_listings WHERE run_id = ?",
        (output_fixture["run_id"],),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == 1


@pytest.mark.asyncio
async def test_exploring_worker_fanout(repo, tmp_path):
    first = load_fixture("qualification_input.json")
    second = load_fixture("qualification_input.json")
    second["run_id"] = "test-run-002"
    second["listing"]["job_id"] = "9999999"
    second["listing"]["job_url"] = "https://example.com/jobs/9999999"

    queue = InMemoryQueueBackend()
    config = WorkerConfig(EXPLORING_Q, SCORING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = ExploringWorker(
        stage=MockStage(_result(True, [first, second])),
        repo=repo,
        logger=logging.getLogger("test.exploring.fanout"),
        artifacts_root=tmp_path,
        config=config,
    )
    msg = make_worker_message({"run_id": "seed", "max_listings": 2}, EXPLORING_Q, SCORING_Q)
    await queue.enqueue(EXPLORING_Q, msg)
    pulled = await queue.pull(EXPLORING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    m1 = await queue.pull(SCORING_Q)
    m2 = await queue.pull(SCORING_Q)
    assert m1 is not None and m2 is not None


@pytest.mark.asyncio
async def test_exploring_worker_stage_failure_routes_to_dlq(repo, tmp_path):
    queue = InMemoryQueueBackend()
    config = WorkerConfig(EXPLORING_Q, SCORING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = ExploringWorker(
        stage=MockStage(_result(False, None, "failed")),
        repo=repo,
        logger=logging.getLogger("test.exploring.fail"),
        artifacts_root=tmp_path,
        config=config,
    )
    msg = make_worker_message({"run_id": "seed", "max_listings": 1}, EXPLORING_Q, SCORING_Q)
    await queue.enqueue(EXPLORING_Q, msg)
    pulled = await queue.pull(EXPLORING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    assert await queue.pull(SCORING_Q) is None
    dlq = await queue.pull(DEAD_LETTER_Q)
    assert dlq is not None


@pytest.mark.asyncio
async def test_exploring_worker_unhandled_exception_nacks(repo, tmp_path):
    queue = InMemoryQueueBackend()
    config = WorkerConfig(EXPLORING_Q, SCORING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = ExploringWorker(
        stage=_ExplodingStage(),
        repo=repo,
        logger=logging.getLogger("test.exploring.ex"),
        artifacts_root=tmp_path,
        config=config,
    )
    msg = make_worker_message({"run_id": "seed", "max_listings": 1}, EXPLORING_Q, SCORING_Q)
    await queue.enqueue(EXPLORING_Q, msg)
    pulled = await queue.pull(EXPLORING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    nacked = await queue.pull(EXPLORING_Q)
    assert nacked is not None
    assert await queue.pull(SCORING_Q) is None
    assert await queue.pull(DEAD_LETTER_Q) is None


@pytest.mark.asyncio
async def test_exploring_worker_fanout_is_per_source(repo, tmp_path):
    stage = ExploringStage(
        AppConfig(),
        scrapers={
            "source_a": _MockScraper([_listing("Acme", "1", "source_a"), _listing("Acme", "2", "source_a")]),
            "source_b": _MockScraper([_listing("Beta", "3", "source_b"), _listing("Beta", "4", "source_b")]),
        },
    )
    queue = InMemoryQueueBackend()
    config = WorkerConfig(EXPLORING_Q, SCORING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = ExploringWorker(
        stage=stage,
        repo=repo,
        logger=logging.getLogger("test.exploring.per_source"),
        artifacts_root=tmp_path,
        config=config,
    )
    msg = make_worker_message(
        {"run_id": "seed", "max_listings": 1, "search_config": {"platforms": ["source_a", "source_b"]}},
        EXPLORING_Q,
        SCORING_Q,
    )
    await queue.enqueue(EXPLORING_Q, msg)
    pulled = await queue.pull(EXPLORING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    first = await queue.pull(SCORING_Q)
    second = await queue.pull(SCORING_Q)
    third = await queue.pull(SCORING_Q)
    assert first is not None and second is not None
    assert third is None


@pytest.mark.asyncio
async def test_exploring_worker_skips_cross_source_duplicates_and_continues(repo, tmp_path):
    duplicate = _listing("Acme", "1", "source_a")
    stage = ExploringStage(
        AppConfig(),
        scrapers={
            "source_a": _MockScraper([duplicate]),
            "source_b": _MockScraper([duplicate, _listing("Beta", "2", "source_b")]),
        },
    )
    queue = InMemoryQueueBackend()
    config = WorkerConfig(EXPLORING_Q, SCORING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = ExploringWorker(
        stage=stage,
        repo=repo,
        logger=logging.getLogger("test.exploring.dedupe"),
        artifacts_root=tmp_path,
        config=config,
    )
    msg = make_worker_message(
        {"run_id": "seed", "max_listings": 2, "search_config": {"platforms": ["source_a", "source_b"]}},
        EXPLORING_Q,
        SCORING_Q,
    )
    await queue.enqueue(EXPLORING_Q, msg)
    pulled = await queue.pull(EXPLORING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    first = await queue.pull(SCORING_Q)
    second = await queue.pull(SCORING_Q)
    third = await queue.pull(SCORING_Q)
    assert first is not None and second is not None
    assert third is None
    run_ids = {first.run_id, second.run_id}
    assert run_ids == {"acme_1", "beta_2"}
