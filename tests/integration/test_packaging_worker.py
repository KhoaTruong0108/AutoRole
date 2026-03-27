from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from autorole.context import JobApplicationContext
from autorole.queue import DEAD_LETTER_Q, InMemoryQueueBackend, PACKAGING_Q, SESSION_Q
from autorole.workers.base import WorkerConfig
from autorole.workers.packaging import PackagingWorker
from tests.conftest import MockStage, load_fixture, make_worker_message


class _ExplodingStage:
    async def execute(self, message: object) -> object:
        _ = message
        raise RuntimeError("boom")


def _result(success: bool, output: object = None, error: str = "") -> SimpleNamespace:
    return SimpleNamespace(success=success, output=output, error=error)


@pytest.mark.asyncio
async def test_packaging_worker_success(repo, tmp_path):
    input_fixture = load_fixture("packaging_input.json")
    output_fixture = load_fixture("session_input.json")

    queue = InMemoryQueueBackend()
    config = WorkerConfig(PACKAGING_Q, SESSION_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = PackagingWorker(
        stage=MockStage(_result(True, output_fixture)),
        repo=repo,
        logger=logging.getLogger("test.packaging"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, PACKAGING_Q, SESSION_Q)
    await queue.enqueue(PACKAGING_Q, msg)
    pulled = await queue.pull(PACKAGING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    out = await queue.pull(SESSION_Q)
    assert out is not None
    out_ctx = JobApplicationContext.model_validate(out.payload)
    assert out_ctx.packaged is not None
    assert await queue.pull(DEAD_LETTER_Q) is None

    checkpoint = await repo.get_checkpoint(input_fixture["run_id"])
    assert checkpoint is not None and checkpoint[0] == "packaging"


@pytest.mark.asyncio
async def test_packaging_worker_stage_failure_routes_to_dlq(repo, tmp_path):
    input_fixture = load_fixture("packaging_input.json")

    queue = InMemoryQueueBackend()
    config = WorkerConfig(PACKAGING_Q, SESSION_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = PackagingWorker(
        stage=MockStage(_result(False, None, "render failed")),
        repo=repo,
        logger=logging.getLogger("test.packaging.fail"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, PACKAGING_Q, SESSION_Q)
    await queue.enqueue(PACKAGING_Q, msg)
    pulled = await queue.pull(PACKAGING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    assert await queue.pull(SESSION_Q) is None
    assert await queue.pull(DEAD_LETTER_Q) is not None


@pytest.mark.asyncio
async def test_packaging_worker_unhandled_exception_nacks(repo, tmp_path):
    input_fixture = load_fixture("packaging_input.json")

    queue = InMemoryQueueBackend()
    config = WorkerConfig(PACKAGING_Q, SESSION_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = PackagingWorker(
        stage=_ExplodingStage(),
        repo=repo,
        logger=logging.getLogger("test.packaging.ex"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, PACKAGING_Q, SESSION_Q)
    await queue.enqueue(PACKAGING_Q, msg)
    pulled = await queue.pull(PACKAGING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    nacked = await queue.pull(PACKAGING_Q)
    assert nacked is not None
    assert await queue.pull(SESSION_Q) is None
    assert await queue.pull(DEAD_LETTER_Q) is None
