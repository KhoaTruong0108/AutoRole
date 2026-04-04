from __future__ import annotations

from typing import Any

from .._snapflow import BlockedError, ErrorCategory, Gate, StateContext
from ..stage_ids import CONCLUDING, FORM_SCRAPER


class FormSubmissionGate(Gate):
    def __init__(self, *, max_loops: int = 2) -> None:
        if max_loops < 1:
            raise ValueError("max_loops must be >= 1")
        self._max_loops = max_loops

    def evaluate(self, ctx: StateContext[Any]) -> str | None:
        payload = self._extract_submission_payload(ctx.data)
        decision = str(payload.get("decision", "pass"))

        if decision == "pass":
            return CONCLUDING

        if decision == "block":
            raise BlockedError(
                reason=str(payload.get("reason", "Form submission blocked")),
                category=ErrorCategory.BUSINESS_RULE,
            )

        loop_count = int(payload.get("loop_count", int(ctx.attempt) + 1))
        if loop_count > self._max_loops:
            raise BlockedError(
                reason=f"Form submission loop exceeded max loops ({loop_count})",
                category=ErrorCategory.BUSINESS_RULE,
            )
        return FORM_SCRAPER

    @staticmethod
    def _extract_submission_payload(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise BlockedError(
                reason="Form submission gate expected dict payload",
                category=ErrorCategory.VALIDATION,
            )

        candidate = data.get("form_submission")
        if not isinstance(candidate, dict):
            raise BlockedError(
                reason="Form submission gate missing form_submission payload",
                category=ErrorCategory.VALIDATION,
            )
        return candidate
