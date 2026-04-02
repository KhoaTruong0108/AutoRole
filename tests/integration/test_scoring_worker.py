from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from autorole.context import JobApplicationContext
from autorole.queue import DEAD_LETTER_Q, PACKAGING_Q, SCORING_Q, TAILORING_Q
from autorole.workers.base import WorkerConfig
from autorole.workers.scoring import ScoringWorker
from autorole.workers.tailoring import TailoringWorker
from tests.conftest import MockStage, load_fixture, make_worker_message, queue_row_count


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
async def test_scoring_worker_success(repo, queue_backend, tmp_path):
    input_fixture = load_fixture("exploration_seed_input.json")
    output_fixture = load_fixture("packaging_input.json")
    output_fixture.pop("tailored", None)
    output_ctx = JobApplicationContext.model_validate(output_fixture)

    queue = queue_backend
    config = WorkerConfig(SCORING_Q, TAILORING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = ScoringWorker(
        scoring_stage=MockStage(_result(True, output_ctx)),
        repo=repo,
        logger=logging.getLogger("test.scoring"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, SCORING_Q, TAILORING_Q)
    await queue.enqueue(SCORING_Q, msg)
    pulled = await queue.pull(SCORING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    out = await queue.pull(TAILORING_Q)
    assert out is not None
    result_ctx = JobApplicationContext.model_validate(out.payload)
    assert result_ctx.score is not None
    assert await queue.pull(DEAD_LETTER_Q) is None

    checkpoint = await repo.get_checkpoint(output_fixture["run_id"])
    assert checkpoint is not None
    assert checkpoint[0] == "scoring"


@pytest.mark.asyncio
async def test_tailoring_worker_loop_then_pass(repo, queue_backend, tmp_path):
    input_fixture = load_fixture("packaging_input.json")
    loop_ctx = load_fixture("packaging_input.json")
    loop_ctx["tailored"]["tailoring_degree"] = 1
    pass_ctx = load_fixture("packaging_input.json")
    pass_ctx["tailored"]["tailoring_degree"] = 0

    tailoring = _SequenceStage([_result(True, loop_ctx), _result(True, pass_ctx)])

    queue = queue_backend
    config = WorkerConfig(TAILORING_Q, PACKAGING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = TailoringWorker(
        tailoring_stage=tailoring,
        repo=repo,
        logger=logging.getLogger("test.tailoring.loop"),
        artifacts_root=tmp_path,
        config=config,
        max_attempts=3,
    )

    msg = make_worker_message(input_fixture, TAILORING_Q, PACKAGING_Q)
    await queue.enqueue(TAILORING_Q, msg)
    first = await queue.pull(TAILORING_Q)
    assert first is not None
    await worker.process(queue, first)

    loop_msg = await queue.pull(TAILORING_Q)
    assert loop_msg is not None
    await worker.process(queue, loop_msg)

    out = await queue.pull(PACKAGING_Q)
    assert out is not None


@pytest.mark.asyncio
async def test_tailoring_worker_block_after_max_attempts(repo, queue_backend, tmp_path):
    input_fixture = load_fixture("packaging_input.json")
    loop_ctx = load_fixture("packaging_input.json")
    loop_ctx["tailored"]["tailoring_degree"] = 1

    tailoring = _SequenceStage([_result(True, loop_ctx), _result(True, loop_ctx)])

    queue = queue_backend
    config = WorkerConfig(TAILORING_Q, PACKAGING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = TailoringWorker(
        tailoring_stage=tailoring,
        repo=repo,
        logger=logging.getLogger("test.tailoring.block"),
        artifacts_root=tmp_path,
        config=config,
        max_attempts=1,
    )

    msg = make_worker_message(input_fixture, TAILORING_Q, PACKAGING_Q)
    await queue.enqueue(TAILORING_Q, msg)
    first = await queue.pull(TAILORING_Q)
    assert first is not None
    await worker.process(queue, first)

    second = await queue.pull(TAILORING_Q)
    assert second is not None
    await worker.process(queue, second)

    assert await queue.pull(PACKAGING_Q) is None
    dlq = await queue.pull(DEAD_LETTER_Q)
    assert dlq is not None


@pytest.mark.asyncio
async def test_scoring_worker_stage_failure_routes_to_dlq(repo, queue_backend, tmp_path):
    input_fixture = load_fixture("exploration_seed_input.json")
    queue = queue_backend
    config = WorkerConfig(SCORING_Q, TAILORING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = ScoringWorker(
        scoring_stage=MockStage(_result(False, None, "bad score")),
        repo=repo,
        logger=logging.getLogger("test.scoring.fail"),
        artifacts_root=tmp_path,
        config=config,
    )
    msg = make_worker_message(input_fixture, SCORING_Q, TAILORING_Q)
    await queue.enqueue(SCORING_Q, msg)
    pulled = await queue.pull(SCORING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    assert await queue.pull(TAILORING_Q) is None
    assert await queue.pull(DEAD_LETTER_Q) is not None


@pytest.mark.asyncio
async def test_tailoring_worker_unhandled_exception_nacks(repo, queue_backend, db, tmp_path):
    input_fixture = load_fixture("packaging_input.json")
    queue = queue_backend
    config = WorkerConfig(TAILORING_Q, PACKAGING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = TailoringWorker(
        tailoring_stage=_ExplodingStage(),
        repo=repo,
        logger=logging.getLogger("test.tailoring.ex"),
        artifacts_root=tmp_path,
        config=config,
        max_attempts=2,
    )
    msg = make_worker_message(input_fixture, TAILORING_Q, PACKAGING_Q)
    await queue.enqueue(TAILORING_Q, msg)
    pulled = await queue.pull(TAILORING_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    assert await queue_row_count(db, TAILORING_Q) == 1
    assert await queue.pull(PACKAGING_Q) is None
    assert await queue.pull(DEAD_LETTER_Q) is None


@pytest.mark.asyncio
async def test_tailoring_worker_duplicate_message_is_acknowledged(repo, queue_backend, tmp_path):
    input_fixture = load_fixture("packaging_input.json")
    output_fixture = load_fixture("packaging_input.json")
    output_fixture["tailored"]["tailoring_degree"] = 0
    output_ctx = JobApplicationContext.model_validate(output_fixture)

    worker = TailoringWorker(
        tailoring_stage=MockStage(_result(True, output_ctx)),
        repo=repo,
        logger=logging.getLogger("test.tailoring.duplicate"),
        artifacts_root=tmp_path,
        config=WorkerConfig(TAILORING_Q, PACKAGING_Q, DEAD_LETTER_Q, poll_interval_seconds=0),
        max_attempts=2,
    )
    queue = queue_backend

    first = make_worker_message(input_fixture, TAILORING_Q, PACKAGING_Q)
    second = make_worker_message(input_fixture, TAILORING_Q, PACKAGING_Q)
    await queue.enqueue(TAILORING_Q, first)
    await queue.enqueue(TAILORING_Q, second)

    pulled_first = await queue.pull(TAILORING_Q)
    assert pulled_first is not None
    await worker.process(queue, pulled_first)

    pulled_second = await queue.pull(TAILORING_Q)
    assert pulled_second is not None
    await worker.process(queue, pulled_second)

    first_out = await queue.pull(PACKAGING_Q)
    second_out = await queue.pull(PACKAGING_Q)
    assert first_out is not None
    assert second_out is not None
    assert await queue.pull(DEAD_LETTER_Q) is None
