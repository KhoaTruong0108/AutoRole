from __future__ import annotations

import asyncio
from dataclasses import dataclass

from autorole_next._snapflow import StateContext
from autorole_next.executors.concluding import ConcludingExecutor


@dataclass
class _FakeStore:
    calls: list[dict[str, object]]

    async def finalize_application_projection(
        self,
        correlation_id: str,
        *,
        final_score: float,
        resume_path: str,
        pdf_path: str,
    ) -> None:
        self.calls.append(
            {
                "correlation_id": correlation_id,
                "final_score": final_score,
                "resume_path": resume_path,
                "pdf_path": pdf_path,
            }
        )


def _ctx(data: dict[str, object], metadata: dict[str, object] | None = None) -> StateContext[dict[str, object]]:
    return StateContext[dict[str, object]](
        correlation_id="corr-concluding-1",
        current_stage="concluding",
        data=data,
        metadata={} if metadata is None else metadata,
    )


def _result_payload(result: object) -> dict[str, object]:
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        return data
    output = getattr(result, "output", None)
    if isinstance(output, dict):
        return output
    raise AssertionError("stage result payload is missing")


def test_concluding_executor_shuts_down_shared_browser(monkeypatch) -> None:
    async def _fake_shutdown_shared_browser(shared_browser: dict[str, object]) -> dict[str, object]:
        closed = dict(shared_browser)
        closed["status"] = "closed"
        closed["closed_remote"] = True
        return closed

    monkeypatch.setattr("autorole_next.executors.concluding.shutdown_shared_browser", _fake_shutdown_shared_browser)
    monkeypatch.setattr(ConcludingExecutor, "_write_concluding_artifact", staticmethod(lambda correlation_id, payload: None))

    store = _FakeStore(calls=[])
    ConcludingExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = ConcludingExecutor()

    result = asyncio.run(
        executor.execute(
            _ctx(
                {
                    "scoring": {"overall_score": 0.91},
                    "packaging": {
                        "resume_path": "resumes/corr-concluding-1/tailored.md",
                        "pdf_path": "resumes/corr-concluding-1/tailored.pdf",
                    },
                    "form_submission": {"status": "submitted"},
                    "session": {
                        "shared_browser": {
                            "kind": "shared_browser",
                            "status": "ready",
                            "endpoint": "http://127.0.0.1:2242",
                            "pid": 999,
                        }
                    },
                    "shared_browser": {
                        "kind": "shared_browser",
                        "status": "ready",
                        "endpoint": "http://127.0.0.1:2242",
                        "pid": 999,
                    },
                }
            )
        )
    )

    assert result.success is True
    payload = _result_payload(result)

    final_payload = payload.get("concluding")
    assert isinstance(final_payload, dict)
    assert final_payload.get("submission_status") == "submitted"
    assert final_payload.get("shared_browser_status") == "closed"

    shared_browser = payload.get("shared_browser")
    assert isinstance(shared_browser, dict)
    assert shared_browser.get("status") == "closed"

    assert len(store.calls) == 1
    assert store.calls[0]["final_score"] == 0.91