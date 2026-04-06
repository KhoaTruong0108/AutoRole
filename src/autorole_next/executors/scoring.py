from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from autorole_next.config import AppConfig, ScoringConfig

from .._snapflow import Executor, StageResult, StateContext
from ..scoring.strategies import get_scoring_strategy, split_matched
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
        try:
            score_payload = await self._build_score_payload(payload, ctx.metadata)
        except ValueError as exc:
            return StageResult.fail(str(exc), "ValidationError")
        except Exception as exc:
            return StageResult.fail(str(exc), exc.__class__.__name__)
        payload["scoring"] = score_payload

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
    async def _build_score_payload(payload: dict[str, Any], metadata: dict[str, Any]) -> dict[str, Any]:
        app_config = AppConfig()
        previous_attempt = 0
        previous_scoring = payload.get("scoring")
        if isinstance(previous_scoring, dict):
            raw_attempt = previous_scoring.get("attempt", 0)
            if isinstance(raw_attempt, int):
                previous_attempt = raw_attempt

        forced = metadata.get("forced_score")
        if isinstance(forced, (int, float)):
            overall = float(max(0.0, min(1.0, forced)))
            criteria_scores = {
                "technical_skills": overall,
                "experience_depth": overall,
                "seniority_alignment": overall,
                "domain_relevance": overall,
                "culture_fit": overall,
            }
            matched, mismatched = split_matched(criteria_scores)
            return {
                "attempt": previous_attempt + 1,
                "strategy": "forced",
                "overall_score": round(overall, 4),
                "criteria_scores": criteria_scores,
                "matched": matched,
                "mismatched": mismatched,
                "jd_summary": f"Scored at {_utcnow_iso()}",
            }

        scoring_config = _resolve_scoring_config(metadata, app_config)
        strategy_name = _resolve_scoring_strategy(metadata, scoring_config)
        strategy = get_scoring_strategy(strategy_name)
        strategy_payload = await strategy.score(
            payload=payload,
            metadata=metadata,
            config=scoring_config,
            app_config=app_config,
        )
        return {
            **strategy_payload,
            "attempt": previous_attempt + 1,
            "scored_at": _utcnow_iso(),
        }


def _resolve_scoring_config(metadata: dict[str, Any], app_config: AppConfig) -> ScoringConfig:
    scoring_override = metadata.get("scoring_config")
    if isinstance(scoring_override, ScoringConfig):
        return scoring_override
    if isinstance(scoring_override, dict):
        try:
            return ScoringConfig.model_validate(scoring_override)
        except Exception:
            return app_config.scoring
    return app_config.scoring


def _resolve_scoring_strategy(metadata: dict[str, Any], config: ScoringConfig) -> str:
    strategy_override = metadata.get("scoring_strategy")
    if isinstance(strategy_override, str) and strategy_override.strip():
        return strategy_override.strip().lower()
    return str(config.strategy).strip().lower()