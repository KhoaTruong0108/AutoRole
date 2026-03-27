from __future__ import annotations

from collections.abc import Callable

from autorole.context import JobApplicationContext
from autorole.workers.base import StageWorker


class ConcludingWorker(StageWorker):
    name = "concluding"

    def __init__(self, *args: object, done_callback: Callable[[], None] | None = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._done_callback = done_callback

    async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = attempt
        self._write_artifact("output.txt", "Concluding stage completed successfully.\n", ctx.run_id)
        await self._repo.upsert_application(
            run_id=ctx.run_id,
            listing=ctx.listing,
            score=ctx.score,
            tailored=ctx.tailored,
            packaged=ctx.packaged,
            applied=ctx.applied,
        )
        if self._done_callback is not None:
            self._done_callback()

    def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = (ctx, attempt)
        print("[ok] concluding -> job application persisted")
