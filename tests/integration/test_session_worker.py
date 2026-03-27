from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from autorole.context import JobApplicationContext
from autorole.queue import DEAD_LETTER_Q, FORM_INTEL_Q, InMemoryQueueBackend, SESSION_Q
from autorole.workers.base import WorkerConfig
from autorole.workers.session import SessionWorker
from tests.conftest import MockStage, load_fixture, make_worker_message


class _ExplodingStage:
    async def execute(self, message: object) -> object:
        _ = message
        raise RuntimeError("boom")


def _result(success: bool, output: object = None, error: str = "") -> SimpleNamespace:
    return SimpleNamespace(success=success, output=output, error=error)


@pytest.mark.asyncio
async def test_session_worker_success(repo, tmp_path):
    input_fixture = load_fixture("session_input.json")
    output_fixture = load_fixture("form_intelligence_input.json")

    queue = InMemoryQueueBackend()
    config = WorkerConfig(SESSION_Q, FORM_INTEL_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = SessionWorker(
        stage=MockStage(_result(True, output_fixture)),
        repo=repo,
        logger=logging.getLogger("test.session"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, SESSION_Q, FORM_INTEL_Q)
    await queue.enqueue(SESSION_Q, msg)
    pulled = await queue.pull(SESSION_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    out = await queue.pull(FORM_INTEL_Q)
    assert out is not None
    out_ctx = JobApplicationContext.model_validate(out.payload)
    assert out_ctx.session is not None
    assert await queue.pull(DEAD_LETTER_Q) is None

    checkpoint = await repo.get_checkpoint(input_fixture["run_id"])
    assert checkpoint is not None and checkpoint[0] == "session"


@pytest.mark.asyncio
async def test_session_worker_stage_failure_routes_to_dlq(repo, tmp_path):
    input_fixture = load_fixture("session_input.json")

    queue = InMemoryQueueBackend()
    config = WorkerConfig(SESSION_Q, FORM_INTEL_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = SessionWorker(
        stage=MockStage(_result(False, None, "session failed")),
        repo=repo,
        logger=logging.getLogger("test.session.fail"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, SESSION_Q, FORM_INTEL_Q)
    await queue.enqueue(SESSION_Q, msg)
    pulled = await queue.pull(SESSION_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    assert await queue.pull(FORM_INTEL_Q) is None
    assert await queue.pull(DEAD_LETTER_Q) is not None


@pytest.mark.asyncio
async def test_session_worker_unhandled_exception_nacks(repo, tmp_path):
    input_fixture = load_fixture("session_input.json")

    queue = InMemoryQueueBackend()
    config = WorkerConfig(SESSION_Q, FORM_INTEL_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = SessionWorker(
        stage=_ExplodingStage(),
        repo=repo,
        logger=logging.getLogger("test.session.ex"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, SESSION_Q, FORM_INTEL_Q)
    await queue.enqueue(SESSION_Q, msg)
    pulled = await queue.pull(SESSION_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    nacked = await queue.pull(SESSION_Q)
    assert nacked is not None
    assert await queue.pull(FORM_INTEL_Q) is None
    assert await queue.pull(DEAD_LETTER_Q) is None
