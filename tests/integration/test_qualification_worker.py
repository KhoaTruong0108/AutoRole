from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from autorole.context import JobApplicationContext
from autorole.queue import DEAD_LETTER_Q, InMemoryQueueBackend, PACKAGING_Q, SCORING_Q
from autorole.workers.base import WorkerConfig
from autorole.workers.qualification import QualificationWorker
from tests.conftest import MockStage, load_fixture, make_worker_message


class _ExplodingStage:
    async def execute(self, message: object) -> object:
        _ = message
        raise RuntimeError("boom")


class _SequenceStage:
    def __init__(self, results: list[SimpleNamespace]) -> None:
        self._results = results
        self._idx = 0

    async def execute(self, message: object) -> SimpleNamespace:
        _ = message
        current = self._results[min(self._idx, len(self._results) - 1)]
        self._idx += 1
        return current


def _result(success: bool, output: object = None, error: str = "") -> SimpleNamespace:
    return SimpleNamespace(success=success, output=output, error=error)


@pytest.mark.asyncio
async def test_qualification_worker_success(repo, tmp_path):
    input_fixture = load_fixture("qualification_input.json")
    output_fixture = load_fixture("packaging_input.json")
    output_fixture["tailored"]["tailoring_degree"] = 0

    queue = InMemoryQueueBackend()
    config = WorkerConfig(SCORING_Q, PACKAGING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = QualificationWorker(
        scoring_stage=MockStage(_result(True, output_fixture)),
        tailoring_stage=MockStage(_result(True, output_fixture)),
        repo=repo,
        logger=logging.getLogger("test.qualification"),
        artifacts_root=tmp_path,
        config=config,
        max_attempts=3,
    )

    msg = make_worker_message(input_fixture, SCORING_Q, PACKAGING_Q)
    await queue.enqueue(SCORING_Q, msg)
    pulled = await queue.pull(SCORING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    out = await queue.pull(PACKAGING_Q)
    assert out is not None
    result_ctx = JobApplicationContext.model_validate(out.payload)
    assert result_ctx.tailored is not None
    assert await queue.pull(DEAD_LETTER_Q) is None

    checkpoint = await repo.get_checkpoint(input_fixture["run_id"])
    assert checkpoint is not None
    assert checkpoint[0] == "qualification"


@pytest.mark.asyncio
async def test_qualification_worker_loop_then_pass(repo, tmp_path):
    input_fixture = load_fixture("qualification_input.json")
    loop_ctx = load_fixture("packaging_input.json")
    pass_ctx = load_fixture("packaging_input.json")
    pass_ctx["tailored"]["tailoring_degree"] = 0

    scoring = _SequenceStage([_result(True, loop_ctx), _result(True, pass_ctx)])
    tailoring = _SequenceStage([_result(True, loop_ctx), _result(True, pass_ctx)])

    queue = InMemoryQueueBackend()
    config = WorkerConfig(SCORING_Q, PACKAGING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = QualificationWorker(
        scoring_stage=scoring,
        tailoring_stage=tailoring,
        repo=repo,
        logger=logging.getLogger("test.qualification.loop"),
        artifacts_root=tmp_path,
        config=config,
        max_attempts=3,
    )

    msg = make_worker_message(input_fixture, SCORING_Q, PACKAGING_Q)
    await queue.enqueue(SCORING_Q, msg)
    first = await queue.pull(SCORING_Q)
    assert first is not None
    await worker.process(queue, first)

    loop_msg = await queue.pull(SCORING_Q)
    assert loop_msg is not None
    await worker.process(queue, loop_msg)

    out = await queue.pull(PACKAGING_Q)
    assert out is not None


@pytest.mark.asyncio
async def test_qualification_worker_block_after_max_attempts(repo, tmp_path):
    input_fixture = load_fixture("qualification_input.json")
    loop_ctx = load_fixture("packaging_input.json")

    scoring = _SequenceStage([_result(True, loop_ctx), _result(True, loop_ctx)])
    tailoring = _SequenceStage([_result(True, loop_ctx), _result(True, loop_ctx)])

    queue = InMemoryQueueBackend()
    config = WorkerConfig(SCORING_Q, PACKAGING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = QualificationWorker(
        scoring_stage=scoring,
        tailoring_stage=tailoring,
        repo=repo,
        logger=logging.getLogger("test.qualification.block"),
        artifacts_root=tmp_path,
        config=config,
        max_attempts=1,
    )

    msg = make_worker_message(input_fixture, SCORING_Q, PACKAGING_Q)
    await queue.enqueue(SCORING_Q, msg)
    first = await queue.pull(SCORING_Q)
    assert first is not None
    await worker.process(queue, first)

    second = await queue.pull(SCORING_Q)
    assert second is not None
    await worker.process(queue, second)

    assert await queue.pull(PACKAGING_Q) is None
    dlq = await queue.pull(DEAD_LETTER_Q)
    assert dlq is not None


@pytest.mark.asyncio
async def test_qualification_worker_stage_failure_routes_to_dlq(repo, tmp_path):
    input_fixture = load_fixture("qualification_input.json")
    queue = InMemoryQueueBackend()
    config = WorkerConfig(SCORING_Q, PACKAGING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = QualificationWorker(
        scoring_stage=MockStage(_result(False, None, "bad score")),
        tailoring_stage=MockStage(_result(True, None)),
        repo=repo,
        logger=logging.getLogger("test.qualification.fail"),
        artifacts_root=tmp_path,
        config=config,
        max_attempts=2,
    )
    msg = make_worker_message(input_fixture, SCORING_Q, PACKAGING_Q)
    await queue.enqueue(SCORING_Q, msg)
    pulled = await queue.pull(SCORING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    assert await queue.pull(PACKAGING_Q) is None
    assert await queue.pull(DEAD_LETTER_Q) is not None


@pytest.mark.asyncio
async def test_qualification_worker_unhandled_exception_nacks(repo, tmp_path):
    input_fixture = load_fixture("qualification_input.json")
    queue = InMemoryQueueBackend()
    config = WorkerConfig(SCORING_Q, PACKAGING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = QualificationWorker(
        scoring_stage=_ExplodingStage(),
        tailoring_stage=MockStage(_result(True, None)),
        repo=repo,
        logger=logging.getLogger("test.qualification.ex"),
        artifacts_root=tmp_path,
        config=config,
        max_attempts=2,
    )
    msg = make_worker_message(input_fixture, SCORING_Q, PACKAGING_Q)
    await queue.enqueue(SCORING_Q, msg)
    pulled = await queue.pull(SCORING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    nacked = await queue.pull(SCORING_Q)
    assert nacked is not None
    assert await queue.pull(PACKAGING_Q) is None
    assert await queue.pull(DEAD_LETTER_Q) is None
