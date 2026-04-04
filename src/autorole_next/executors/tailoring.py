from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._snapflow import Executor, StageResult, StateContext
from ..store import AutoRoleStoreAdapter


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TailoringExecutor(Executor[dict[str, Any]]):
    _store: AutoRoleStoreAdapter | None = None

    @classmethod
    def configure_store(cls, store: AutoRoleStoreAdapter) -> None:
        cls._store = store

    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        scoring = payload.get("scoring") if isinstance(payload.get("scoring"), dict) else {}
        overall = float(scoring.get("overall_score", 0.0))
        attempt = int(scoring.get("attempt", int(ctx.attempt) + 1))
        degree = self._select_degree(overall)
        resume_path = f"resumes/{ctx.correlation_id}/tailored_v{attempt}.md"
        self._materialize_tailored_resume(
            resume_path,
            correlation_id=ctx.correlation_id,
            attempt=attempt,
            degree=degree,
        )

        tailoring_payload = {
            "tailoring_degree": degree,
            "resume_path": resume_path,
            "diff_summary": f"Tailoring degree {degree} generated at {_utcnow_iso()}",
            "tailored_at": _utcnow_iso(),
        }
        payload["tailoring"] = tailoring_payload
        payload["tailored"] = tailoring_payload

        store = self._store
        if store is None:
            raise RuntimeError("TailoringExecutor store is not configured")

        await store.append_tailored_resume(
            ctx.correlation_id,
            attempt=attempt,
            resume_path=resume_path,
            diff_summary=str(tailoring_payload["diff_summary"]),
            tailoring_degree=degree,
        )
        return StageResult.ok(payload)

    @staticmethod
    def _materialize_tailored_resume(
        resume_path: str,
        *,
        correlation_id: str,
        attempt: int,
        degree: int,
    ) -> None:
        target = Path(resume_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return
        target.write_text(
            "\n".join(
                [
                    f"# Tailored Resume ({correlation_id})",
                    "",
                    f"- attempt: {attempt}",
                    f"- tailoring_degree: {degree}",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _select_degree(score: float) -> int:
        if score >= 0.55:
            return 0
        if score >= 0.50:
            return 1
        if score >= 0.40:
            return 2
        return 3