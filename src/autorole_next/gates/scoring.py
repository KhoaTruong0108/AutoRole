from __future__ import annotations

from typing import Any

from .._snapflow import BlockedError, ErrorCategory, Gate, StateContext
from ..stage_ids import TAILORING


class ScoringGate(Gate):
    def evaluate(self, ctx: StateContext[Any]) -> str | None:
        self._extract_score_payload(ctx.data)

        return TAILORING

    @staticmethod
    def _extract_score_payload(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise BlockedError(
                reason="Scoring gate expected dict payload",
                category=ErrorCategory.VALIDATION,
            )

        candidate = data.get("scoring") or data.get("score")
        if not isinstance(candidate, dict):
            raise BlockedError(
                reason="Scoring gate missing scoring payload",
                category=ErrorCategory.VALIDATION,
            )
        return candidate