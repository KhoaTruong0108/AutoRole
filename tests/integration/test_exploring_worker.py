from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from autorole.context import JobApplicationContext
from autorole.queue import DEAD_LETTER_Q, EXPLORING_Q, InMemoryQueueBackend, SCORING_Q
from autorole.workers.base import WorkerConfig
from autorole.workers.exploring import ExploringWorker
from tests.conftest import MockStage, load_fixture, make_worker_message


class _ExplodingStage:
    async def execute(self, message: object) -> object:
        _ = message
        raise RuntimeError("boom")


def _result(success: bool, output: object = None, error: str = "") -> SimpleNamespace:
    return SimpleNamespace(success=success, output=output, error=error)


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
