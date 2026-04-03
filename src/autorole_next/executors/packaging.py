from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .._snapflow import Executor, StageResult, StateContext


class PackagingExecutor(Executor[dict[str, Any]]):
    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        payload["packaging"] = {
            "packaged_at": datetime.now(timezone.utc).isoformat(),
            "status": "ready",
        }
        return StageResult.ok(payload)
