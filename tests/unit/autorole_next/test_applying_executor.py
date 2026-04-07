from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from autorole_next._snapflow import StateContext
from autorole_next.executors.llm_applying import ApplyingExecutor


@dataclass
class _FakeStore:
    calls: list[dict[str, object]]

    async def upsert_application_status(
        self,
        correlation_id: str,
        *,
        status: str,
    ) -> None:
        self.calls.append({"method": "status", "correlation_id": correlation_id, "status": status})

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
                "method": "submission",
                "correlation_id": correlation_id,
                "status": status,
                "confirmed": confirmed,
                "applied_at": applied_at,
            }
        )


def _ctx(data: dict[str, object]) -> StateContext[dict[str, object]]:
    return StateContext[dict[str, object]](
        correlation_id="corr-apply-1",
        current_stage="llm_applying",
        data=data,
    )


def test_applying_executor_requires_form_submission_or_packaging_payload() -> None:
    store = _FakeStore(calls=[])
    ApplyingExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = ApplyingExecutor()

    result = asyncio.run(executor.execute(_ctx({})))

    assert result.success is False
    assert result.error_type == "PreconditionError"


def test_applying_executor_promotes_submitted_to_applied() -> None:
    store = _FakeStore(calls=[])
    ApplyingExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = ApplyingExecutor()

    payload = {
        "form_submission": {
            "status": "submitted",
            "reason": "submission completed",
            "confirmed": True,
        },
        "applied": {
            "submission_status": "submitted",
        },
    }

    result = asyncio.run(executor.execute(_ctx(payload)))

    assert result.success is True
    data = dict(result.data)
    llm_applying = data.get("llm_applying")
    assert isinstance(llm_applying, dict)
    assert "applying" not in data
    assert llm_applying.get("status") == "applied"
    assert llm_applying.get("source_status") == "submitted"
    assert llm_applying.get("confirmed") is True

    assert store.calls == [
        {"method": "status", "correlation_id": "corr-apply-1", "status": "applied"},
        {
            "method": "submission",
            "correlation_id": "corr-apply-1",
            "status": "applied",
            "confirmed": True,
            "applied_at": llm_applying.get("completed_at"),
        },
    ]


def test_applying_executor_preserves_non_submitted_status() -> None:
    store = _FakeStore(calls=[])
    ApplyingExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = ApplyingExecutor()

    payload = {
        "form_submission": {
            "status": "dry_run",
            "reason": "dry-run submission simulated",
            "confirmed": False,
        },
    }

    result = asyncio.run(executor.execute(_ctx(payload)))

    assert result.success is True
    data = dict(result.data)
    llm_applying = data.get("llm_applying")
    assert isinstance(llm_applying, dict)
    assert "applying" not in data
    assert llm_applying.get("status") == "dry_run"
    assert llm_applying.get("source_status") == "dry_run"

    assert store.calls == [
        {"method": "status", "correlation_id": "corr-apply-1", "status": "dry_run"},
        {
            "method": "submission",
            "correlation_id": "corr-apply-1",
            "status": "dry_run",
            "confirmed": False,
            "applied_at": llm_applying.get("completed_at"),
        },
    ]


async def _fake_runner(**_: Any) -> dict[str, Any]:
    return {
        "status": "applied",
        "source_status": "applied",
        "confirmed": True,
        "reason": "claude completed application",
        "completed_at": "2026-04-05T12:00:00+00:00",
        "log_path": "logs/llm_applying/corr-apply-1/claude-output.txt",
        "mcp_config_path": "logs/llm_applying/corr-apply-1/mcp-config.json",
        "prompt_path": "logs/llm_applying/corr-apply-1/prompt.txt",
        "raw_result_line": "RESULT:APPLIED",
        "tool_events": ["mcp__playwright__browser_navigate"],
    }


def test_applying_executor_supports_packaging_alternative_flow() -> None:
    store = _FakeStore(calls=[])
    ApplyingExecutor.configure_store(store)  # type: ignore[arg-type]
    ApplyingExecutor.configure_runner(_fake_runner)
    executor = ApplyingExecutor()

    result = asyncio.run(
        executor.execute(
            StateContext[dict[str, object]](
                correlation_id="corr-apply-1",
                current_stage="llm_applying",
                data={
                    "listing": {
                        "job_url": "https://example.com/jobs/1",
                        "apply_url": "https://example.com/jobs/1/apply",
                        "company_name": "Example",
                        "job_title": "Staff Engineer",
                        "platform": "greenhouse",
                    },
                    "packaging": {
                        "status": "ready",
                        "resume_path": "resumes/corr-apply-1/tailored.md",
                        "pdf_path": "resumes/corr-apply-1/tailored.pdf",
                    }
                },
                metadata={"profile_path": "/tmp/user_profile.json"},
            )
        )
    )

    assert result.success is True
    data = dict(result.data)
    llm_applying = data.get("llm_applying")
    assert isinstance(llm_applying, dict)
    assert "applying" not in data
    assert llm_applying.get("status") == "applied"
    assert llm_applying.get("confirmed") is True
    assert llm_applying.get("raw_result_line") == "RESULT:APPLIED"

    assert store.calls == [
        {"method": "status", "correlation_id": "corr-apply-1", "status": "applied"},
        {
            "method": "submission",
            "correlation_id": "corr-apply-1",
            "status": "applied",
            "confirmed": True,
            "applied_at": "2026-04-05T12:00:00+00:00",
        },
    ]
