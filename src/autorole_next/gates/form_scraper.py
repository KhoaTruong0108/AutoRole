from __future__ import annotations

from typing import Any

from .._snapflow import Gate, StateContext
from ..stage_ids import FIELD_COMPLETER


class FormScraperGate(Gate):
    def evaluate(self, ctx: StateContext[Any]) -> str | None:
        _ = ctx
        return FIELD_COMPLETER
