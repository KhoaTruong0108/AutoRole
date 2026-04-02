from __future__ import annotations

import json
from typing import Any

from autorole.context import JobApplicationContext
from autorole.gates.best_fit import BestFitGate
from autorole.queue import Message
from autorole.workers.base import StageWorker
from autorole.workers.policies import BestFitRoutingPolicy


class _TailoringStage:
    def __init__(self, tailoring_stage: Any) -> None:
        self._tailoring = tailoring_stage

    async def execute(self, message: Message) -> Any:
        ctx = JobApplicationContext.model_validate(message.payload)
        working_message = Message(
            run_id=ctx.run_id,
            stage=message.stage,
            payload=ctx.model_dump(mode="json"),
            reply_queue=message.reply_queue,
            dead_letter_queue=message.dead_letter_queue,
            attempt=message.attempt,
            metadata=message.metadata,
        )
        return await self._tailoring.execute(working_message)


class TailoringWorker(StageWorker):
    name = "tailoring"

    def __init__(
        self,
        tailoring_stage: Any,
        *args: Any,
        max_attempts: int = 3,
        **kwargs: Any,
    ) -> None:
        routing = BestFitRoutingPolicy(BestFitGate(max_attempts=max_attempts))
        super().__init__(
            _TailoringStage(tailoring_stage),
            *args,
            routing_policy=routing,
            **kwargs,
        )

    async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
        if ctx.tailored is not None:
            self._write_artifact(
                f"attempt_{attempt}_resume_diff.md",
                (
                    f"# Resume Diff (attempt {attempt})\\n\\n"
                    f"Tailoring degree: {ctx.tailored.tailoring_degree}\\n\\n"
                    f"Source file: {ctx.tailored.file_path}\\n\\n"
                    f"## Diff Summary\\n\\n{ctx.tailored.diff_summary}\\n"
                ),
                ctx.run_id,
            )
            await self._repo.upsert_tailored(ctx.run_id, ctx.tailored)
            self._write_artifact(
                f"attempt_{attempt}_summary.json",
                json.dumps(
                    {
                        "tailoring_degree": ctx.tailored.tailoring_degree,
                        "file_path": ctx.tailored.file_path,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                ctx.run_id,
            )

    def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
        degree = ctx.tailored.tailoring_degree if ctx.tailored is not None else -1
        print(f"[ok] tailoring -> degree={degree} (attempt {attempt})")
