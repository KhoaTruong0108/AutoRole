from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from autorole.context import JobApplicationContext
from autorole.queue import CONCLUDING_Q, DEAD_LETTER_Q, FORM_INTEL_Q, FORM_SUB_Q, InMemoryQueueBackend
from autorole.workers.base import WorkerConfig
from autorole.workers.form_submission import FormSubmissionWorker
from tests.conftest import MockStage, load_fixture, make_worker_message


class _ExplodingStage:
    async def execute(self, message: object) -> object:
        _ = message
        raise RuntimeError("boom")


def _result(success: bool, output: object = None, error: str = "") -> SimpleNamespace:
    return SimpleNamespace(success=success, output=output, error=error)


@pytest.mark.asyncio
async def test_form_submission_worker_loop_requeues_to_form_intel(repo, tmp_path):
    input_fixture = load_fixture("form_submission_input.json")

    queue = InMemoryQueueBackend()
    config = WorkerConfig(FORM_SUB_Q, CONCLUDING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = FormSubmissionWorker(
        stage=MockStage(_result(True, input_fixture)),
        repo=repo,
        logger=logging.getLogger("test.form_sub.loop"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, FORM_SUB_Q, CONCLUDING_Q)
    await queue.enqueue(FORM_SUB_Q, msg)
    pulled = await queue.pull(FORM_SUB_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    loop_msg = await queue.pull(FORM_INTEL_Q)
    assert loop_msg is not None
    assert await queue.pull(FORM_SUB_Q) is None
    assert await queue.pull(CONCLUDING_Q) is None


@pytest.mark.asyncio
async def test_form_submission_worker_pass_to_concluding(repo, tmp_path):
    input_fixture = load_fixture("form_submission_input.json")
    pass_output = load_fixture("concluding_input.json")

    queue = InMemoryQueueBackend()
    config = WorkerConfig(FORM_SUB_Q, CONCLUDING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = FormSubmissionWorker(
        stage=MockStage(_result(True, pass_output)),
        repo=repo,
        logger=logging.getLogger("test.form_sub.pass"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, FORM_SUB_Q, CONCLUDING_Q)
    await queue.enqueue(FORM_SUB_Q, msg)
    pulled = await queue.pull(FORM_SUB_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    out = await queue.pull(CONCLUDING_Q)
    assert out is not None
    out_ctx = JobApplicationContext.model_validate(out.payload)
    assert out_ctx.applied is not None
    assert await queue.pull(DEAD_LETTER_Q) is None


@pytest.mark.asyncio
async def test_form_submission_worker_stage_failure_routes_to_dlq(repo, tmp_path):
    input_fixture = load_fixture("form_submission_input.json")

    queue = InMemoryQueueBackend()
    config = WorkerConfig(FORM_SUB_Q, CONCLUDING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = FormSubmissionWorker(
        stage=MockStage(_result(False, None, "submission failed")),
        repo=repo,
        logger=logging.getLogger("test.form_sub.fail"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, FORM_SUB_Q, CONCLUDING_Q)
    await queue.enqueue(FORM_SUB_Q, msg)
    pulled = await queue.pull(FORM_SUB_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    assert await queue.pull(CONCLUDING_Q) is None
    assert await queue.pull(DEAD_LETTER_Q) is not None


@pytest.mark.asyncio
async def test_form_submission_worker_unhandled_exception_nacks(repo, tmp_path):
    input_fixture = load_fixture("form_submission_input.json")

    queue = InMemoryQueueBackend()
    config = WorkerConfig(FORM_SUB_Q, CONCLUDING_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = FormSubmissionWorker(
        stage=_ExplodingStage(),
        repo=repo,
        logger=logging.getLogger("test.form_sub.ex"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, FORM_SUB_Q, CONCLUDING_Q)
    await queue.enqueue(FORM_SUB_Q, msg)
    pulled = await queue.pull(FORM_SUB_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    nacked = await queue.pull(FORM_SUB_Q)
    assert nacked is not None
    assert await queue.pull(CONCLUDING_Q) is None
    assert await queue.pull(DEAD_LETTER_Q) is None
