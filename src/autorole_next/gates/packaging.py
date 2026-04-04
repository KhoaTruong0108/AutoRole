from __future__ import annotations

from typing import Any

from .._snapflow import Gate, StateContext
from ..stage_ids import SESSION


class PackagingGate(Gate):
    def evaluate(self, ctx: StateContext[Any]) -> str | None:
        _ = ctx
        return SESSION
