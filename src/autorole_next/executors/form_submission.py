from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._snapflow import Executor, StageResult, StateContext
from ..store import AutoRoleStoreAdapter


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FormSubmissionExecutor(Executor[dict[str, Any]]):
    _store: AutoRoleStoreAdapter | None = None

    @classmethod
    def configure_store(cls, store: AutoRoleStoreAdapter) -> None:
        cls._store = store

    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        metadata = dict(ctx.metadata)
        previous_submission = payload.get("form_submission") if isinstance(payload.get("form_submission"), dict) else {}

        loop_count = int(previous_submission.get("loop_count", 0))
        force_loop = bool(metadata.get("force_form_loop", False))
        guardrail_block = bool(metadata.get("submit_disabled", False))
        apply_mode = str(metadata.get("apply_mode", "dry_run"))

        decision = "pass"
        reason = "submission completed"
        status = "submitted"
        confirmed = True
        if guardrail_block:
            decision = "block"
            reason = "submission disabled by operator guardrail"
            status = "submit_disabled"
            confirmed = False
        elif force_loop and loop_count == 0:
            decision = "loop"
            reason = "additional scrape cycle requested"
            status = "rescrape_required"
            confirmed = False
            loop_count = 1
        elif apply_mode == "dry_run":
            decision = "pass"
            reason = "dry-run submission simulated"
            status = "dry_run"
            confirmed = False

        audit_path = self._write_audit_log(
            correlation_id=ctx.correlation_id,
            payload={
                "decision": decision,
                "reason": reason,
                "apply_mode": apply_mode,
                "timestamp": _utcnow_iso(),
            },
        )

        submission_payload = {
            "decision": decision,
            "reason": reason,
            "status": status,
            "confirmed": confirmed,
            "loop_count": int(loop_count),
            "audit_log_path": audit_path,
            "submitted_at": _utcnow_iso(),
        }
        payload["form_submission"] = submission_payload

        store = self._store
        if store is None:
            raise RuntimeError("FormSubmissionExecutor store is not configured")

        await store.upsert_application_submission(
            ctx.correlation_id,
            status=status,
            confirmed=confirmed,
            applied_at=submission_payload["submitted_at"],
        )

        return StageResult.ok(payload)

    @staticmethod
    def _write_audit_log(*, correlation_id: str, payload: dict[str, Any]) -> str:
        path = Path("logs") / "form_submission" / correlation_id / "audit.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return str(path)
