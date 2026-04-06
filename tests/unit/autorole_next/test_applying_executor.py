from __future__ import annotations

import asyncio
from dataclasses import dataclass

from autorole_next._snapflow import StateContext
from autorole_next.executors.applying import ApplyingExecutor


@dataclass
class _FakeStore:
    calls: list[dict[str, object]]

    async def upsert_application_status(
        self,
        correlation_id: str,
        *,
        status: str,
    ) -> None:
        self.calls.append({"correlation_id": correlation_id, "status": status})


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
    assert llm_applying.get("status") == "applied"
    assert llm_applying.get("source_status") == "submitted"
    assert llm_applying.get("confirmed") is True

    assert store.calls == [{"correlation_id": "corr-apply-1", "status": "applied"}]


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
    assert llm_applying.get("status") == "dry_run"
    assert llm_applying.get("source_status") == "dry_run"

    assert store.calls == [{"correlation_id": "corr-apply-1", "status": "dry_run"}]


def test_applying_executor_supports_packaging_alternative_flow() -> None:
    store = _FakeStore(calls=[])
    ApplyingExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = ApplyingExecutor()

    result = asyncio.run(
        executor.execute(
            StateContext[dict[str, object]](
                correlation_id="corr-apply-1",
                current_stage="llm_applying",
                data={
                    "packaging": {
                        "status": "ready",
                        "pdf_path": "resumes/corr-apply-1/tailored.pdf",
                    }
                },
                metadata={"llm_applying_status": "applied", "llm_applying_confirmed": True},
            )
        )
    )

    assert result.success is True
    data = dict(result.data)
    llm_applying = data.get("llm_applying")
    assert isinstance(llm_applying, dict)
    assert llm_applying.get("status") == "applied"
    assert llm_applying.get("confirmed") is True

    assert store.calls == [{"correlation_id": "corr-apply-1", "status": "applied"}]
