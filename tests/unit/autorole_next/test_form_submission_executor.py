from __future__ import annotations

import asyncio
from dataclasses import dataclass

from autorole_next._snapflow import StateContext
from autorole_next.executors.form_submission import FormSubmissionExecutor


@dataclass
class _FakeStore:
    calls: list[dict[str, object]]

    async def upsert_application_submission(
        self,
        correlation_id: str,
        *,
        status: str,
        confirmed: bool,
        applied_at: str,
    ) -> None:
        self.calls.append(
            {
                "correlation_id": correlation_id,
                "status": status,
                "confirmed": confirmed,
                "applied_at": applied_at,
            }
        )


def _ctx(data: dict[str, object], metadata: dict[str, object] | None = None) -> StateContext[dict[str, object]]:
    return StateContext[dict[str, object]](
        correlation_id="corr-form-sub-1",
        current_stage="formSubmission",
        data=data,
        metadata={} if metadata is None else metadata,
    )


def _base_payload() -> dict[str, object]:
    return {
        "listing": {
            "job_url": "https://example.com/jobs/1",
            "apply_url": "https://example.com/jobs/1/apply",
            "platform": "workday",
        },
        "formScraper": {
            "page_index": 0,
            "page_label": "Application Form",
            "extracted_fields": [
                {
                    "id": "full_name",
                    "field_type": "text",
                    "selector": "#full_name",
                    "label": "Full Name",
                    "required": True,
                }
            ],
            "fill_instructions": [
                {
                    "field_id": "full_name",
                    "action": "fill",
                    "value": "Test User",
                    "source": "generated",
                }
            ],
        },
        "fieldCompleter": {
            "fill_instructions": [
                {
                    "field_id": "full_name",
                    "action": "fill",
                    "value": "Test User",
                    "source": "generated",
                }
            ]
        },
        "form_session": {
            "detection": {
                "run_id": "corr-form-sub-1",
                "platform_id": "workday",
                "apply_url": "https://example.com/jobs/1/apply",
                "used_iframe": False,
                "detection_method": "url",
            },
            "page_index": 0,
            "all_fields": [],
            "all_instructions": [],
            "all_outcomes": [],
            "last_advance_action": "next_page",
            "screenshots": [],
        },
        "packaged": {
            "resume_path": "resumes/corr-form-sub-1/tailored.md",
            "pdf_path": "resumes/corr-form-sub-1/tailored.pdf",
        },
    }


def test_form_submission_executor_fails_without_required_inputs() -> None:
    store = _FakeStore(calls=[])
    FormSubmissionExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = FormSubmissionExecutor()

    result = asyncio.run(executor.execute(_ctx({"listing": {}})))

    assert result.success is False
    assert result.error_type == "PreconditionError"


def test_form_submission_executor_dry_run_succeeds_without_page() -> None:
    store = _FakeStore(calls=[])
    FormSubmissionExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = FormSubmissionExecutor()

    result = asyncio.run(executor.execute(_ctx(_base_payload(), metadata={"apply_mode": "dry_run"})))

    assert result.success is True
    data = dict(result.data)

    submission = data.get("form_submission")
    assert isinstance(submission, dict)
    assert submission.get("status") == "dry_run"
    assert submission.get("decision") == "pass"
    assert submission.get("confirmed") is False

    session = data.get("form_session")
    assert isinstance(session, dict)
    assert session.get("last_advance_action") == "done"

    assert len(store.calls) == 1
    assert store.calls[0]["status"] == "dry_run"
    assert store.calls[0]["confirmed"] is False


def test_form_submission_executor_submit_disabled_marks_block_decision() -> None:
    store = _FakeStore(calls=[])
    FormSubmissionExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = FormSubmissionExecutor()

    result = asyncio.run(executor.execute(_ctx(_base_payload(), metadata={"submit_disabled": True})))

    assert result.success is True
    data = dict(result.data)

    submission = data.get("form_submission")
    assert isinstance(submission, dict)
    assert submission.get("status") == "submit_disabled"
    assert submission.get("decision") == "block"
    assert submission.get("confirmed") is False

    assert len(store.calls) == 1
    assert store.calls[0]["status"] == "submit_disabled"
    assert store.calls[0]["confirmed"] is False
