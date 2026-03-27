from __future__ import annotations

import json
from typing import Any

from autorole.context import JobApplicationContext
from autorole.gates.best_fit import BestFitGate
from autorole.queue import Message
from autorole.workers.base import StageWorker
from autorole.workers.policies import BestFitRoutingPolicy


class _QualificationStage:
    def __init__(self, scoring_stage: Any, tailoring_stage: Any) -> None:
        self._scoring = scoring_stage
        self._tailoring = tailoring_stage

    async def execute(self, message: Message) -> Any:
        score_result = await self._scoring.execute(message)
        if not getattr(score_result, "success", False):
            return score_result

        score_ctx = JobApplicationContext.model_validate(score_result.output)
        tail_msg = Message(
            run_id=message.run_id,
            stage=message.stage,
            payload=score_ctx.model_dump(mode="json"),
            reply_queue=message.reply_queue,
            dead_letter_queue=message.dead_letter_queue,
            attempt=message.attempt,
            metadata=message.metadata,
        )
        return await self._tailoring.execute(tail_msg)


class QualificationWorker(StageWorker):
    name = "qualification"

    def __init__(
        self,
        scoring_stage: Any,
        tailoring_stage: Any,
        *args: Any,
        max_attempts: int = 3,
        **kwargs: Any,
    ) -> None:
        routing = BestFitRoutingPolicy(BestFitGate(max_attempts=max_attempts))
        super().__init__(_QualificationStage(scoring_stage, tailoring_stage), *args, routing_policy=routing, **kwargs)

    async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
        if ctx.score is not None:
            self._write_artifact(
                f"attempt_{attempt}_summary.json",
                json.dumps(
                    {
                        "overall_score": ctx.score.overall_score,
                        "criteria_scores": ctx.score.criteria_scores,
                        "matched": ctx.score.matched,
                        "mismatched": ctx.score.mismatched,
                        "jd_breakdown": ctx.score.jd_breakdown,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                ctx.run_id,
            )
            await self._repo.upsert_score(ctx.run_id, ctx.score, attempt)

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

    def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
        score = ctx.score.overall_score if ctx.score is not None else 0.0
        degree = ctx.tailored.tailoring_degree if ctx.tailored is not None else -1
        print(f"[ok] qualification -> score={score:.3f} degree={degree} (attempt {attempt})")
