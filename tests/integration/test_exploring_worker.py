from __future__ import annotations

import logging
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from autorole.config import AppConfig
from autorole.context import ExplorationSeed
from autorole.context import JobListing
from autorole.queue import DEAD_LETTER_Q, EXPLORING_Q, SCORING_Q
from autorole.stages.exploring import ExploringStage
from autorole.workers.base import WorkerConfig
from autorole.workers.exploring import ExploringWorker
from tests.conftest import MockStage, load_fixture, make_worker_message, queue_row_count


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
async def test_exploring_worker_success(repo, queue_backend, tmp_path):
    output_fixture = load_fixture("exploration_seed_input.json")
    queue = queue_backend
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
    out_seed = ExplorationSeed.model_validate(out.payload)
    assert out_seed.listing is not None
    assert await queue.pull(DEAD_LETTER_Q) is None

    async with repo._db.execute("SELECT COUNT(*) FROM job_listings") as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == 0


@pytest.mark.asyncio
async def test_exploring_worker_fanout(repo, queue_backend, tmp_path):
    first = load_fixture("exploration_seed_input.json")
    second = load_fixture("exploration_seed_input.json")
    second["listing"]["job_id"] = "9999999"
    second["listing"]["job_url"] = "https://example.com/jobs/9999999"
    second["listing"]["apply_url"] = "https://example.com/jobs/9999999"

    queue = queue_backend
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
async def test_exploring_worker_stage_failure_routes_to_dlq(repo, queue_backend, tmp_path):
    queue = queue_backend
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
async def test_exploring_worker_unhandled_exception_nacks(repo, queue_backend, db, tmp_path):
    queue = queue_backend
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

    assert await queue_row_count(db, EXPLORING_Q) == 1
    assert await queue.pull(SCORING_Q) is None
    assert await queue.pull(DEAD_LETTER_Q) is None


@pytest.mark.asyncio
async def test_exploring_worker_fanout_is_per_source(repo, queue_backend, tmp_path):
    stage = ExploringStage(
        AppConfig(),
        scrapers={
            "source_a": _MockScraper([_listing("Acme", "1", "source_a"), _listing("Acme", "2", "source_a")]),
            "source_b": _MockScraper([_listing("Beta", "3", "source_b"), _listing("Beta", "4", "source_b")]),
        },
    )
    queue = queue_backend
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
async def test_exploring_worker_keeps_cross_source_duplicates_for_qualification(repo, queue_backend, tmp_path):
    duplicate = _listing("Acme", "1", "source_a")
    stage = ExploringStage(
        AppConfig(),
        scrapers={
            "source_a": _MockScraper([duplicate]),
            "source_b": _MockScraper([duplicate, _listing("Beta", "2", "source_b")]),
        },
    )
    queue = queue_backend
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
    fourth = await queue.pull(SCORING_Q)
    assert first is not None and second is not None and third is not None
    assert fourth is None
