from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
import warnings

from .._snapflow import Executor, StageResult, StateContext
from ..store import AutoRoleStoreAdapter


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LlmApplyingExecutor(Executor[dict[str, Any]]):
    _store: AutoRoleStoreAdapter | None = None

    @classmethod
    def configure_store(cls, store: AutoRoleStoreAdapter) -> None:
        cls._store = store

    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        metadata = dict(ctx.metadata)

        submission = payload.get("form_submission") if isinstance(payload.get("form_submission"), dict) else None
        packaged = payload.get("packaged") if isinstance(payload.get("packaged"), dict) else None
        if packaged is None and isinstance(payload.get("packaging"), dict):
            packaged = dict(payload.get("packaging"))

        if submission is None and packaged is None:
            return StageResult.fail(
                "LlmApplyingExecutor: form_submission or packaging payload must be set",
                "PreconditionError",
            )

        applied = payload.get("applied") if isinstance(payload.get("applied"), dict) else {}
        if submission is not None:
            source_status = str(submission.get("status") or "")
            final_status = source_status
            if str(applied.get("submission_status") or "").lower() == "submitted":
                final_status = "applied"
            confirmed = bool(submission.get("confirmed", False))
            reason = str(submission.get("reason") or "")
        else:
            source_status = str(
                metadata.get("llm_applying_status")
                or metadata.get("llm_apply_status")
                or "applied"
            )
            final_status = source_status
            confirmed = bool(
                metadata.get("llm_applying_confirmed")
                if "llm_applying_confirmed" in metadata
                else metadata.get("llm_apply_confirmed", final_status == "applied")
            )
            reason = str(
                metadata.get("llm_applying_reason")
                or metadata.get("llm_apply_reason")
                or "llm applying flow from packaging"
            )
            if any(key in metadata for key in ("llm_apply_status", "llm_apply_confirmed", "llm_apply_reason")):
                warnings.warn(
                    "Metadata keys 'llm_apply_*' are deprecated; use 'llm_applying_*' instead.",
                    DeprecationWarning,
                    stacklevel=2,
                )

        applying_payload = {
            "status": final_status,
            "source_status": source_status,
            "confirmed": confirmed,
            "reason": reason,
            "completed_at": _utcnow_iso(),
        }
        payload["llm_applying"] = applying_payload

        store = self._store
        if store is None:
            raise RuntimeError("LlmApplyingExecutor store is not configured")

        await store.upsert_application_status(
            ctx.correlation_id,
            status=final_status,
        )

        return StageResult.ok(payload)
