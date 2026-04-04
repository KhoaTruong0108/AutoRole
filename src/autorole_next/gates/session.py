from __future__ import annotations

from typing import Any

from .._snapflow import Gate, StateContext
from ..stage_ids import FORM_SCRAPER


class SessionGate(Gate):
    def evaluate(self, ctx: StateContext[Any]) -> str | None:
        _ = ctx
        return FORM_SCRAPER
