from __future__ import annotations

from typing import Any

from .._snapflow import BlockedError, ErrorCategory, Gate, StateContext
from ..stage_ids import PACKAGING, SCORING


class TailoringGate(Gate):
    def __init__(self, *, max_attempts: int = 3) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._max_attempts = max_attempts

    def evaluate(self, ctx: StateContext[Any]) -> str | None:
        tailoring_payload = self._extract_tailoring_payload(ctx.data)
        degree = int(tailoring_payload.get("tailoring_degree", 0))
        if degree == 0:
            return PACKAGING

        score_payload = self._extract_score_payload(ctx.data)
        attempt_number = int(score_payload.get("attempt", int(ctx.attempt) + 1))
        if attempt_number >= self._max_attempts:
            raise BlockedError(
                reason=(
                    f"Tailoring blocked after {attempt_number} attempts "
                    f"with degree={degree}"
                ),
                category=ErrorCategory.BUSINESS_RULE,
            )

        return SCORING

    @staticmethod
    def _extract_tailoring_payload(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise BlockedError(
                reason="Tailoring gate expected dict payload",
                category=ErrorCategory.VALIDATION,
            )

        candidate = data.get("tailoring") or data.get("tailored")
        if not isinstance(candidate, dict):
            raise BlockedError(
                reason="Tailoring gate missing tailoring payload",
                category=ErrorCategory.VALIDATION,
            )
        return candidate

    @staticmethod
    def _extract_score_payload(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise BlockedError(
                reason="Tailoring gate expected dict payload",
                category=ErrorCategory.VALIDATION,
            )

        candidate = data.get("scoring")
        if not isinstance(candidate, dict):
            raise BlockedError(
                reason="Tailoring gate missing scoring payload",
                category=ErrorCategory.VALIDATION,
            )
        return candidate