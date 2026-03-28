from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from autorole.queue import CONCLUDING_Q, DEAD_LETTER_Q, InMemoryQueueBackend
from autorole.workers.base import WorkerConfig
from autorole.workers.concluding import ConcludingWorker
from tests.conftest import MockStage, load_fixture, make_worker_message


class _ExplodingStage:
    async def execute(self, message: object) -> object:
        _ = message
        raise RuntimeError("boom")


def _result(success: bool, output: object = None, error: str = "") -> SimpleNamespace:
    return SimpleNamespace(success=success, output=output, error=error)


@pytest.mark.asyncio
async def test_concluding_worker_success_calls_done_callback(repo, tmp_path):
    input_fixture = load_fixture("concluding_input.json")
    called = {"value": False}

    def done_callback() -> None:
        called["value"] = True

    queue = InMemoryQueueBackend()
    config = WorkerConfig(CONCLUDING_Q, CONCLUDING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = ConcludingWorker(
        stage=MockStage(_result(True, input_fixture)),
        repo=repo,
        logger=logging.getLogger("test.concluding"),
        artifacts_root=tmp_path,
        config=config,
        done_callback=done_callback,
    )

    msg = make_worker_message(input_fixture, CONCLUDING_Q, CONCLUDING_Q)
    await queue.enqueue(CONCLUDING_Q, msg)
    pulled = await queue.pull(CONCLUDING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    assert called["value"] is True
    checkpoint = await repo.get_checkpoint(input_fixture["run_id"])
    assert checkpoint is not None and checkpoint[0] == "concluding"

    async with repo._db.execute(
        "SELECT submission_status FROM job_applications WHERE run_id = ?",
        (input_fixture["run_id"],),
    ) as cur:
        row = await cur.fetchone()
    assert row is not None
    assert row[0] is None


@pytest.mark.asyncio
async def test_concluding_worker_stage_failure_routes_to_dlq(repo, tmp_path):
    input_fixture = load_fixture("concluding_input.json")

    queue = InMemoryQueueBackend()
    config = WorkerConfig(CONCLUDING_Q, CONCLUDING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = ConcludingWorker(
        stage=MockStage(_result(False, None, "concluding failed")),
        repo=repo,
        logger=logging.getLogger("test.concluding.fail"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, CONCLUDING_Q, CONCLUDING_Q)
    await queue.enqueue(CONCLUDING_Q, msg)
    pulled = await queue.pull(CONCLUDING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    assert await queue.pull(DEAD_LETTER_Q) is not None


@pytest.mark.asyncio
async def test_concluding_worker_unhandled_exception_nacks(repo, tmp_path):
    input_fixture = load_fixture("concluding_input.json")

    queue = InMemoryQueueBackend()
    config = WorkerConfig(CONCLUDING_Q, CONCLUDING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = ConcludingWorker(
        stage=_ExplodingStage(),
        repo=repo,
        logger=logging.getLogger("test.concluding.ex"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, CONCLUDING_Q, CONCLUDING_Q)
    await queue.enqueue(CONCLUDING_Q, msg)
    pulled = await queue.pull(CONCLUDING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    nacked = await queue.pull(CONCLUDING_Q)
    assert nacked is not None
    assert await queue.pull(DEAD_LETTER_Q) is None
