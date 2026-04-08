from __future__ import annotations

import asyncio
from dataclasses import dataclass

from autorole_next._snapflow import StateContext
from autorole_next.executors.session import SessionExecutor


@dataclass
class _FakeStore:
    calls: list[dict[str, object]]

    async def upsert_session(
        self,
        correlation_id: str,
        *,
        platform: str,
        authenticated: bool,
        session_note: str,
    ) -> None:
        self.calls.append(
            {
                "correlation_id": correlation_id,
                "platform": platform,
                "authenticated": authenticated,
                "session_note": session_note,
            }
        )


def _ctx(data: dict[str, object], metadata: dict[str, object] | None = None) -> StateContext[dict[str, object]]:
    return StateContext[dict[str, object]](
        correlation_id="corr-session-1",
        current_stage="session",
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


def test_session_executor_emits_shared_browser_descriptor(monkeypatch) -> None:
    async def _fake_launch_shared_browser(**_: object) -> dict[str, object]:
        return {
            "kind": "shared_browser",
            "status": "ready",
            "endpoint": "http://127.0.0.1:2242",
            "port": 2242,
            "pid": 999,
        }

    monkeypatch.setattr("autorole_next.executors.session.launch_shared_browser", _fake_launch_shared_browser)

    store = _FakeStore(calls=[])
    SessionExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = SessionExecutor()

    result = asyncio.run(
        executor.execute(
            _ctx(
                {
                    "listing": {
                        "platform": "workday",
                        "apply_url": "https://company.workday.com/jobs/1/apply",
                    }
                }
            )
        )
    )

    assert result.success is True
    payload = _result_payload(result)

    session_payload = payload.get("session")
    assert isinstance(session_payload, dict)
    assert session_payload.get("platform") == "workday"
    assert session_payload.get("authenticated") is False

    shared_browser = payload.get("shared_browser")
    assert isinstance(shared_browser, dict)
    assert shared_browser.get("status") == "ready"
    assert shared_browser.get("endpoint") == "http://127.0.0.1:2242"
    assert session_payload.get("shared_browser") == shared_browser

    assert len(store.calls) == 1
    assert store.calls[0]["platform"] == "workday"
    assert store.calls[0]["authenticated"] is False