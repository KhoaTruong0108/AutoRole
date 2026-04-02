from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from autorole.context import JobApplicationContext
from autorole.queue import DEAD_LETTER_Q, FORM_INTEL_Q, LLM_FIELD_COMPLETER_Q
from autorole.workers.base import WorkerConfig
from autorole.workers.form_intelligence import FormIntelligenceWorker
from tests.conftest import MockStage, load_fixture, make_worker_message, queue_row_count


class _ExplodingStage:
    async def execute(self, message: object) -> object:
        _ = message
        raise RuntimeError("boom")


def _result(success: bool, output: object = None, error: str = "") -> SimpleNamespace:
    return SimpleNamespace(success=success, output=output, error=error)


@pytest.mark.asyncio
async def test_form_intelligence_worker_success(repo, queue_backend, tmp_path):
    input_fixture = load_fixture("form_intelligence_input.json")
    output_fixture = load_fixture("llm_field_completer_input.json")

    queue = queue_backend
    config = WorkerConfig(FORM_INTEL_Q, LLM_FIELD_COMPLETER_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = FormIntelligenceWorker(
        stage=MockStage(_result(True, output_fixture)),
        repo=repo,
        logger=logging.getLogger("test.form_intel"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, FORM_INTEL_Q, LLM_FIELD_COMPLETER_Q)
    await queue.enqueue(FORM_INTEL_Q, msg)
    pulled = await queue.pull(FORM_INTEL_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    out = await queue.pull(LLM_FIELD_COMPLETER_Q)
    assert out is not None
    out_ctx = JobApplicationContext.model_validate(out.payload)
    assert out_ctx.form_intelligence is not None
    assert await queue.pull(DEAD_LETTER_Q) is None

    checkpoint = await repo.get_checkpoint(input_fixture["run_id"])
    assert checkpoint is not None and checkpoint[0] == "form_intelligence"


@pytest.mark.asyncio
async def test_form_intelligence_worker_stage_failure_routes_to_dlq(repo, queue_backend, tmp_path):
    input_fixture = load_fixture("form_intelligence_input.json")

    queue = queue_backend
    config = WorkerConfig(FORM_INTEL_Q, LLM_FIELD_COMPLETER_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = FormIntelligenceWorker(
        stage=MockStage(_result(False, None, "intel failed")),
        repo=repo,
        logger=logging.getLogger("test.form_intel.fail"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, FORM_INTEL_Q, LLM_FIELD_COMPLETER_Q)
    await queue.enqueue(FORM_INTEL_Q, msg)
    pulled = await queue.pull(FORM_INTEL_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    assert await queue.pull(LLM_FIELD_COMPLETER_Q) is None
    assert await queue.pull(DEAD_LETTER_Q) is not None


@pytest.mark.asyncio
async def test_form_intelligence_worker_unhandled_exception_nacks(repo, queue_backend, db, tmp_path):
    input_fixture = load_fixture("form_intelligence_input.json")

    queue = queue_backend
    config = WorkerConfig(FORM_INTEL_Q, LLM_FIELD_COMPLETER_Q, DEAD_LETTER_Q, poll_interval_seconds=0)
    worker = FormIntelligenceWorker(
        stage=_ExplodingStage(),
        repo=repo,
        logger=logging.getLogger("test.form_intel.ex"),
        artifacts_root=tmp_path,
        config=config,
    )

    msg = make_worker_message(input_fixture, FORM_INTEL_Q, LLM_FIELD_COMPLETER_Q)
    await queue.enqueue(FORM_INTEL_Q, msg)
    pulled = await queue.pull(FORM_INTEL_Q)
    assert pulled is not None

    await worker.process(queue, pulled)

    assert await queue_row_count(db, FORM_INTEL_Q) == 1
    assert await queue.pull(LLM_FIELD_COMPLETER_Q) is None
    assert await queue.pull(DEAD_LETTER_Q) is None
