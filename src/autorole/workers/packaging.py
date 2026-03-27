from __future__ import annotations

import json

from autorole.context import JobApplicationContext
from autorole.workers.base import StageWorker


class PackagingWorker(StageWorker):
    name = "packaging"

    async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = attempt
        if ctx.packaged is None:
            return
        self._write_artifact(
            "output.json",
            json.dumps(ctx.packaged.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            ctx.run_id,
        )

    def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = attempt
        if ctx.packaged is None:
            return
        print(f"[ok] packaging -> pdf={ctx.packaged.pdf_path}")
