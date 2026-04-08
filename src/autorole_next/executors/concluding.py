from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._snapflow import Executor, StageResult, StateContext
from ..integrations.shared_browser import resolve_shared_browser, shared_browser_ready, shutdown_shared_browser
from ..store import AutoRoleStoreAdapter


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConcludingExecutor(Executor[dict[str, Any]]):
    _store: AutoRoleStoreAdapter | None = None

    @classmethod
    def configure_store(cls, store: AutoRoleStoreAdapter) -> None:
        cls._store = store

    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        metadata = dict(ctx.metadata)

        scoring = payload.get("scoring") if isinstance(payload.get("scoring"), dict) else {}
        packaging = payload.get("packaging") if isinstance(payload.get("packaging"), dict) else {}
        submission = payload.get("form_submission") if isinstance(payload.get("form_submission"), dict) else {}
        llm_applying = payload.get("llm_applying") if isinstance(payload.get("llm_applying"), dict) else {}
        shared_browser = resolve_shared_browser(payload, metadata)
        shared_browser_status = "not_requested"
        if shared_browser_ready(shared_browser):
            shared_browser = await shutdown_shared_browser(shared_browser or {})
            shared_browser_status = str(shared_browser.get("status") or "closed")
            payload["shared_browser"] = shared_browser
            if isinstance(payload.get("session"), dict):
                session_payload = dict(payload.get("session"))
                session_payload["shared_browser"] = shared_browser
                payload["session"] = session_payload

        final_payload = {
            "completed_at": _utcnow_iso(),
            "final_score": float(scoring.get("overall_score", 0.0)),
            "resume_path": str(packaging.get("resume_path", "")),
            "pdf_path": str(packaging.get("pdf_path", "")),
            "submission_status": str(submission.get("status") or llm_applying.get("status") or ""),
            "shared_browser_status": shared_browser_status,
        }
        payload["concluding"] = final_payload

        self._write_concluding_artifact(ctx.correlation_id, final_payload)

        store = self._store
        if store is None:
            raise RuntimeError("ConcludingExecutor store is not configured")

        await store.finalize_application_projection(
            ctx.correlation_id,
            final_score=float(final_payload["final_score"]),
            resume_path=str(final_payload["resume_path"]),
            pdf_path=str(final_payload["pdf_path"]),
        )

        return StageResult.ok(payload)

    @staticmethod
    def _write_concluding_artifact(correlation_id: str, payload: dict[str, Any]) -> None:
        path = Path("logs") / "concluding" / correlation_id / "output.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
