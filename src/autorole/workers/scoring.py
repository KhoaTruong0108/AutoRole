from __future__ import annotations

import json
from typing import Any

from autorole.context import ExplorationSeed, JobApplicationContext
from autorole.integrations.discovery.normalization import generate_run_id, normalize_listing
from autorole.queue import Message
from autorole.workers.base import StageWorker


class _ScoringStage:
    def __init__(self, scoring_stage: Any) -> None:
        self._scoring = scoring_stage

    async def execute(self, message: Message) -> Any:
        ingress = await self._build_context(message)

        working_message = Message(
            run_id=ingress.run_id,
            stage=message.stage,
            payload=ingress.model_dump(mode="json"),
            reply_queue=message.reply_queue,
            dead_letter_queue=message.dead_letter_queue,
            attempt=message.attempt,
            metadata=message.metadata,
        )

        return await self._scoring.execute(working_message)

    async def _build_context(self, message: Message) -> JobApplicationContext:
        payload = message.payload
        try:
            existing_ctx = JobApplicationContext.model_validate(payload)
        except Exception:
            existing_ctx = None

        if existing_ctx is not None and existing_ctx.listing is not None and existing_ctx.run_id:
            listing = normalize_listing(existing_ctx.listing)
            return existing_ctx.model_copy(update={"listing": listing})

        seed = ExplorationSeed.model_validate(payload)
        listing = normalize_listing(seed.listing)
        run_id = generate_run_id(listing)
        return JobApplicationContext(run_id=run_id, listing=listing)


class ScoringWorker(StageWorker):
    name = "scoring"

    def __init__(
        self,
        scoring_stage: Any,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            _ScoringStage(scoring_stage),
            *args,
            **kwargs,
        )

    async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
        if ctx.listing is not None:
            await self._repo.upsert_listing(ctx.listing, ctx.run_id)
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

    def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
        score = ctx.score.overall_score if ctx.score is not None else 0.0
        print(f"[ok] scoring -> score={score:.3f} (attempt {attempt})")


# Backward compatibility alias.
QualificationWorker = ScoringWorker
