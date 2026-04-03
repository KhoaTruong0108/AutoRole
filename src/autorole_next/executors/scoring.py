from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .._snapflow import Executor, StageResult, StateContext
from ..store import AutoRoleStoreAdapter


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ScoringExecutor(Executor[dict[str, Any]]):
    _store: AutoRoleStoreAdapter | None = None

    @classmethod
    def configure_store(cls, store: AutoRoleStoreAdapter) -> None:
        cls._store = store

    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        score_payload = self._build_score_payload(payload, ctx.metadata)
        payload["scoring"] = score_payload
        payload["score"] = score_payload

        store = self._store
        if store is None:
            raise RuntimeError("ScoringExecutor store is not configured")

        await store.append_score_report(
            ctx.correlation_id,
            attempt=int(score_payload["attempt"]),
            overall_score=float(score_payload["overall_score"]),
            criteria_scores=dict(score_payload["criteria_scores"]),
            matched=list(score_payload["matched"]),
            mismatched=list(score_payload["mismatched"]),
            jd_summary=str(score_payload["jd_summary"]),
        )
        return StageResult.ok(payload)

    @staticmethod
    def _build_score_payload(payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
        previous_attempt = 0
        previous_scoring = payload.get("scoring")
        if isinstance(previous_scoring, dict):
            raw_attempt = previous_scoring.get("attempt", 0)
            if isinstance(raw_attempt, int):
                previous_attempt = raw_attempt

        forced = metadata.get("forced_score")
        if isinstance(forced, (int, float)):
            overall = float(max(0.0, min(1.0, forced)))
        else:
            listing = payload.get("listing") if isinstance(payload.get("listing"), dict) else {}
            title = str(listing.get("job_title", ""))
            company = str(listing.get("company_name", ""))
            platform = str(listing.get("platform", ""))
            signal = len(title) + len(company) + len(platform)
            overall = min(1.0, 0.35 + (signal % 40) / 100.0)

        criteria_scores = {
            "technical_skills": max(0.0, min(1.0, overall + 0.03)),
            "experience_depth": max(0.0, min(1.0, overall + 0.01)),
            "seniority_alignment": max(0.0, min(1.0, overall - 0.02)),
            "domain_relevance": max(0.0, min(1.0, overall + 0.02)),
            "culture_fit": max(0.0, min(1.0, overall - 0.01)),
        }
        matched = [name for name, value in criteria_scores.items() if value >= 0.7]
        mismatched = [name for name, value in criteria_scores.items() if value < 0.7]
        return {
            "attempt": previous_attempt + 1,
            "overall_score": round(overall, 4),
            "criteria_scores": criteria_scores,
            "matched": matched,
            "mismatched": mismatched,
            "jd_summary": f"Scored at {_utcnow_iso()}",
        }