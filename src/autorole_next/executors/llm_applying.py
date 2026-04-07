from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any
import warnings

from .._snapflow import Executor, StageResult, StateContext
from ..integrations.llm_apply import run_llm_apply
from ..store import AutoRoleStoreAdapter


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LlmApplyingExecutor(Executor[dict[str, Any]]):
    _store: AutoRoleStoreAdapter | None = None
    _runner: Callable[..., Awaitable[dict[str, Any]]] = run_llm_apply

    @classmethod
    def configure_store(cls, store: AutoRoleStoreAdapter) -> None:
        cls._store = store

    @classmethod
    def configure_runner(cls, runner: Callable[..., Awaitable[dict[str, Any]]]) -> None:
        cls._runner = runner

    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        metadata = dict(ctx.metadata)
        listing = payload.get("listing") if isinstance(payload.get("listing"), dict) else None

        submission = payload.get("form_submission") if isinstance(payload.get("form_submission"), dict) else None
        packaged = payload.get("packaging") if isinstance(payload.get("packaging"), dict) else None

        if submission is None and packaged is None:
            return StageResult.fail(
                "LlmApplyingExecutor: form_submission or packaging payload must be set",
                "PreconditionError",
            )

        if packaged is not None:
            if listing is None:
                return StageResult.fail(
                    "LlmApplyingExecutor: listing payload must be set when packaging is present",
                    "PreconditionError",
                )
            try:
                applying_payload = await type(self)._runner(
                    correlation_id=ctx.correlation_id,
                    listing=dict(listing),
                    packaging=dict(packaged),
                    metadata=metadata,
                    payload=payload,
                )
            except Exception as exc:
                return StageResult.fail(f"llm applying runtime failed: {exc}", "ExecutionError")
        else:
            applying_payload = self._coerce_submission_result(payload=payload, metadata=metadata)

        payload["llm_applying"] = applying_payload

        store = self._store
        if store is None:
            raise RuntimeError("LlmApplyingExecutor store is not configured")

        await self._persist_result(store, ctx.correlation_id, applying_payload)

        return StageResult.ok(payload)

    @staticmethod
    def _coerce_submission_result(*, payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
        applied = payload.get("applied") if isinstance(payload.get("applied"), dict) else {}
        submission = payload.get("form_submission") if isinstance(payload.get("form_submission"), dict) else {}
        source_status = str(submission.get("status") or "")
        final_status = source_status
        if str(applied.get("submission_status") or "").lower() == "submitted":
            final_status = "applied"
        confirmed = bool(submission.get("confirmed", False))
        reason = str(submission.get("reason") or "")

        if not source_status:
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

        return {
            "status": final_status,
            "source_status": source_status,
            "confirmed": confirmed,
            "reason": reason,
            "completed_at": _utcnow_iso(),
        }

    @staticmethod
    async def _persist_result(
        store: AutoRoleStoreAdapter,
        correlation_id: str,
        applying_payload: dict[str, Any],
    ) -> None:
        status = str(applying_payload.get("status") or "failed")
        await store.upsert_application_status(correlation_id, status=status)
        if status in {"applied", "dry_run"}:
            await store.upsert_application_submission(
                correlation_id,
                status=status,
                confirmed=bool(applying_payload.get("confirmed", status == "applied")),
                applied_at=str(applying_payload.get("completed_at") or _utcnow_iso()),
            )
