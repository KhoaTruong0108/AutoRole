from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from autorole.context import ExplorationSeed, JobApplicationContext
from autorole.gates.best_fit import BestFitGate
from autorole.integrations.discovery.normalization import canonical_listing_key, generate_run_id, normalize_listing
from autorole.queue import Message
from autorole.workers.base import StageWorker
from autorole.workers.policies import BestFitRoutingPolicy


@dataclass(frozen=True)
class _DuplicateSeedResult:
    run_id: str
    canonical_key: str


class _QualificationStage:
    def __init__(self, repo: Any, scoring_stage: Any, tailoring_stage: Any) -> None:
        self._repo = repo
        self._scoring = scoring_stage
        self._tailoring = tailoring_stage

    async def execute(self, message: Message) -> Any:
        ingress = await self._build_context(message)
        if isinstance(ingress, _DuplicateSeedResult):
            return ingress

        working_message = Message(
            run_id=ingress.run_id,
            stage=message.stage,
            payload=ingress.model_dump(mode="json"),
            reply_queue=message.reply_queue,
            dead_letter_queue=message.dead_letter_queue,
            attempt=message.attempt,
            metadata=message.metadata,
        )

        score_result = await self._scoring.execute(working_message)
        if not getattr(score_result, "success", False):
            return score_result

        score_ctx = JobApplicationContext.model_validate(score_result.output)
        tail_msg = Message(
            run_id=score_ctx.run_id,
            stage=message.stage,
            payload=score_ctx.model_dump(mode="json"),
            reply_queue=message.reply_queue,
            dead_letter_queue=message.dead_letter_queue,
            attempt=message.attempt,
            metadata=message.metadata,
        )
        return await self._tailoring.execute(tail_msg)

    async def _build_context(self, message: Message) -> JobApplicationContext | _DuplicateSeedResult:
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
        canonical_key = canonical_listing_key(listing)
        run_id = generate_run_id(listing)
        claimed = await self._repo.claim_listing_identity(canonical_key, listing, run_id=run_id)
        if not claimed:
            return _DuplicateSeedResult(run_id=run_id, canonical_key=canonical_key)
        return JobApplicationContext(run_id=run_id, listing=listing)


class QualificationWorker(StageWorker):
    name = "qualification"

    def __init__(
        self,
        scoring_stage: Any,
        tailoring_stage: Any,
        *args: Any,
        max_attempts: int = 3,
        on_duplicate: Any | None = None,
        **kwargs: Any,
    ) -> None:
        repo = kwargs["repo"]
        routing = BestFitRoutingPolicy(BestFitGate(max_attempts=max_attempts))
        super().__init__(
            _QualificationStage(repo, scoring_stage, tailoring_stage),
            *args,
            routing_policy=routing,
            **kwargs,
        )
        self._on_duplicate = on_duplicate

    async def process(self, queue: Any, msg: Message) -> None:
        result = await self._execute_inner(msg)

        if result is None:
            current_exec_attempt = self._current_execution_attempt(msg)
            if current_exec_attempt >= self._config.max_attempts:
                reason = f"unhandled exception after {current_exec_attempt} attempt(s)"
                await queue.enqueue(msg.dead_letter_queue, msg)
                await queue.ack(self._config.input_queue, msg.message_id)
                self._logger.error("blocked stage=%s run_id=%s reason=%s", self.name, msg.run_id, reason)
                if self._on_block is not None:
                    self._on_block(msg.run_id, reason)
                return

            retry_msg = self._build_exception_retry_message(msg, current_exec_attempt + 1)
            await queue.enqueue(self._config.input_queue, retry_msg)
            await queue.ack(self._config.input_queue, msg.message_id)
            self._logger.warning(
                "retrying stage=%s run_id=%s next_attempt=%s",
                self.name,
                msg.run_id,
                current_exec_attempt + 1,
            )
            return

        if isinstance(result, _DuplicateSeedResult):
            await queue.ack(self._config.input_queue, msg.message_id)
            self._logger.info(
                "duplicate_listing_skipped stage=%s transport_run_id=%s run_id=%s canonical_key=%s",
                self.name,
                msg.run_id,
                result.run_id,
                result.canonical_key,
            )
            print(f"[skip] qualification -> duplicate listing run_id={result.run_id}")
            if self._on_duplicate is not None:
                self._on_duplicate(result.run_id)
            return

        policy = self._routing_policy
        decision = policy.evaluate(result, msg)

        if decision.decision == "pass":
            if not getattr(result, "success", False):
                await queue.enqueue(msg.dead_letter_queue, msg)
                await queue.ack(self._config.input_queue, msg.message_id)
                if self._on_block is not None:
                    self._on_block(msg.run_id, str(getattr(result, "error", "stage_failed")))
                return

            enriched = self._enrich(msg, result.output)
            await queue.enqueue(msg.reply_queue, enriched)
            await queue.ack(self._config.input_queue, msg.message_id)
            ctx = JobApplicationContext.model_validate(result.output)
            await self.on_success(ctx, msg.attempt)
            await self._repo.upsert_checkpoint(ctx.run_id, self.name, ctx.model_dump(mode="json"))
            self._maybe_export_dryrun_fixture(ctx, msg)
            self.log_ok(ctx, msg.attempt)
            if self._on_pass is not None:
                self._on_pass(ctx.run_id)
            return

        if decision.decision == "loop":
            current_loop_attempt = self._current_loop_attempt(msg)
            if current_loop_attempt >= self._config.max_attempts:
                reason = decision.reason or f"max attempts exceeded ({self._config.max_attempts})"
                await queue.enqueue(msg.dead_letter_queue, msg)
                await queue.ack(self._config.input_queue, msg.message_id)
                self._logger.warning("blocked stage=%s run_id=%s reason=%s", self.name, msg.run_id, reason)
                if self._on_block is not None:
                    self._on_block(msg.run_id, reason)
                return
            loop_msg = self._build_loop_message(msg, decision, result.output)
            await queue.enqueue(self._loop_queue(msg), loop_msg)
            await queue.ack(self._config.input_queue, msg.message_id)
            return

        await queue.enqueue(msg.dead_letter_queue, msg)
        await queue.ack(self._config.input_queue, msg.message_id)
        self._logger.warning("blocked stage=%s run_id=%s reason=%s", self.name, msg.run_id, decision.reason)
        if self._on_block is not None:
            self._on_block(msg.run_id, decision.reason)

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
