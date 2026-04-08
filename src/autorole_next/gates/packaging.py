from __future__ import annotations

import warnings
from typing import Any

from .._snapflow import Gate, StateContext
from ..stage_ids import LLM_APPLYING, SESSION


class PackagingGate(Gate):
    def evaluate(self, ctx: StateContext[Any]) -> str | None:
        metadata = ctx.metadata if isinstance(ctx.metadata, dict) else {}
        apply_mode = str(metadata.get("apply_mode", "")).strip().lower()

        # Alternative apply flow: packaging -> llm_applying -> concluding.
        if apply_mode in {"llm_apply", "direct_llm_apply"}:
            warnings.warn(
                f"apply_mode '{apply_mode}' is deprecated; use '{LLM_APPLYING}' instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            return LLM_APPLYING

        if apply_mode == LLM_APPLYING:
            return LLM_APPLYING

        # return LLM_APPLYING
        return SESSION
