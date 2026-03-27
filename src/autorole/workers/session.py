from __future__ import annotations

import json

from autorole.context import JobApplicationContext
from autorole.workers.base import StageWorker


class SessionWorker(StageWorker):
    name = "session"

    async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = attempt
        if ctx.session is None:
            return
        self._write_artifact(
            "output.json",
            json.dumps(ctx.session.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            ctx.run_id,
        )
        await self._repo.upsert_session(ctx.run_id, ctx.session)

    def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = attempt
        if ctx.session is None:
            return
        print(f"[ok] session -> authenticated={ctx.session.authenticated}")
