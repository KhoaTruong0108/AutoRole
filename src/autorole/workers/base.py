from __future__ import annotations

import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from autorole.context import JobApplicationContext
from autorole.db.repository import JobRepository
from autorole.queue import (
    CONCLUDING_Q,
    EXPLORING_Q,
    FORM_INTEL_Q,
    LLM_FIELD_COMPLETER_Q,
    FORM_SUB_Q,
    PACKAGING_Q,
    SCORING_Q,
    TAILORING_Q,
    SESSION_Q,
    Message,
    QueueBackend,
)

_QUEUE_TO_STAGE: dict[str, str] = {
    EXPLORING_Q: "exploring",
    SCORING_Q: "scoring",
    TAILORING_Q: "tailoring",
    PACKAGING_Q: "packaging",
    SESSION_Q: "session",
    FORM_INTEL_Q: "form_intelligence",
    LLM_FIELD_COMPLETER_Q: "llm_field_completer",
    FORM_SUB_Q: "form_submission",
    CONCLUDING_Q: "concluding",
}

_NEXT_REPLY_QUEUE: dict[str, str] = {
    EXPLORING_Q: SCORING_Q,
    SCORING_Q: TAILORING_Q,
    TAILORING_Q: PACKAGING_Q,
    PACKAGING_Q: SESSION_Q,
    SESSION_Q: FORM_INTEL_Q,
    FORM_INTEL_Q: LLM_FIELD_COMPLETER_Q,
    LLM_FIELD_COMPLETER_Q: FORM_SUB_Q,
    FORM_SUB_Q: CONCLUDING_Q,
    CONCLUDING_Q: CONCLUDING_Q,
}


_DRYRUN_FIXTURE_BY_STAGE: dict[str, str] = {
    "exploring": "qualification_input.json",
    "scoring": "scoring_input.json",
    "tailoring": "packaging_input.json",
    "packaging": "session_input.json",
    "session": "form_intelligence_input.json",
    "form_intelligence": "llm_field_completer_input.json",
    "llm_field_completer": "form_submission_input.json",
    "form_submission": "concluding_input.json",
    "concluding": "concluding_output.json",
}


@dataclass
class WorkerConfig:
    input_queue: str
    reply_queue: str
    dead_letter_queue: str
    poll_interval_seconds: float = 2.0
    visibility_timeout_seconds: int = 300
    max_attempts: int = 3
    execute_timeout_seconds: float = 180.0


@dataclass
class RoutingDecision:
    decision: Literal["pass", "loop", "block"]
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class RoutingPolicy(ABC):
    @abstractmethod
    def evaluate(self, result: Any, message: Message) -> RoutingDecision:
        ...


class StageWorker(ABC):
    name: str

    def __init__(
        self,
        stage: Any,
        repo: JobRepository,
        logger: logging.Logger,
        artifacts_root: Path,
        config: WorkerConfig,
        routing_policy: RoutingPolicy | None = None,
        on_pass: Callable[[str], None] | None = None,
        on_block: Callable[[str, str], None] | None = None,
    ) -> None:
        self._stage = stage
        self._repo = repo
        self._logger = logger
        self._artifacts_root = artifacts_root
        self._config = config
        self._routing_policy = routing_policy
        self._on_pass = on_pass
        self._on_block = on_block

    async def run_forever(self, queue: QueueBackend) -> None:
        await queue.create_queue(self._config.input_queue)
        while True:
            msg = await queue.pull(self._config.input_queue, self._config.visibility_timeout_seconds)
            if msg is None:
                await asyncio.sleep(self._config.poll_interval_seconds)
                continue
            print(
                f"[processing] stage={self.name} queue={self._config.input_queue} "
                f"message_id={msg.message_id} run_id={msg.run_id} attempt={msg.attempt}"
            )
            self._logger.info(
                "processing message stage=%s queue=%s message_id=%s run_id=%s attempt=%s",
                self.name,
                self._config.input_queue,
                msg.message_id,
                msg.run_id,
                msg.attempt,
            )
            try:
                await self.process(queue, msg)
            except Exception as exc:
                print(
                    f"[failed] stage={self.name} queue={self._config.input_queue} "
                    f"message_id={msg.message_id} run_id={msg.run_id} because={exc}"
                )
                self._logger.exception(
                    "failed to process message stage=%s queue=%s message_id=%s run_id=%s because=%s",
                    self.name,
                    self._config.input_queue,
                    msg.message_id,
                    msg.run_id,
                    exc,
                )
                raise
            print(
                f"[finished] stage={self.name} queue={self._config.input_queue} "
                f"message_id={msg.message_id} run_id={msg.run_id}"
            )
            self._logger.info(
                "finished processing message stage=%s queue=%s message_id=%s run_id=%s",
                self.name,
                self._config.input_queue,
                msg.message_id,
                msg.run_id,
            )

    async def process(self, queue: QueueBackend, msg: Message) -> None:
        result = await self._execute_inner(msg)

        if result is None:
            current_exec_attempt = self._current_execution_attempt(msg)
            if current_exec_attempt >= self._config.max_attempts:
                reason = f"unhandled exception after {current_exec_attempt} attempt(s)"
                await queue.enqueue(msg.dead_letter_queue, msg)
                await queue.ack(self._config.input_queue, msg.message_id)
                self._logger.error(
                    "failed to process message stage=%s run_id=%s message_id=%s because=%s",
                    self.name,
                    msg.run_id,
                    msg.message_id,
                    reason,
                )
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

        policy = self._routing_policy
        if policy is None:
            policy = _DefaultPassThroughPolicy()
        decision = policy.evaluate(result, msg)

        if decision.decision == "pass":
            if not getattr(result, "success", False):
                await queue.enqueue(msg.dead_letter_queue, msg)
                await queue.ack(self._config.input_queue, msg.message_id)
                self._logger.warning(
                    "failed to process message stage=%s run_id=%s message_id=%s because=%s",
                    self.name,
                    msg.run_id,
                    msg.message_id,
                    getattr(result, "error", "stage_failed"),
                )
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
                reason = decision.reason or (
                    f"max attempts exceeded ({self._config.max_attempts})"
                )
                await queue.enqueue(msg.dead_letter_queue, msg)
                await queue.ack(self._config.input_queue, msg.message_id)
                self._logger.warning(
                    "failed to process message stage=%s run_id=%s message_id=%s because=%s",
                    self.name,
                    msg.run_id,
                    msg.message_id,
                    reason,
                )
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
        self._logger.warning(
            "failed to process message stage=%s run_id=%s message_id=%s because=%s",
            self.name,
            msg.run_id,
            msg.message_id,
            decision.reason,
        )
        self._logger.warning("blocked stage=%s run_id=%s reason=%s", self.name, msg.run_id, decision.reason)
        if self._on_block is not None:
            self._on_block(msg.run_id, decision.reason)

    def _loop_queue(self, msg: Message) -> str:
        _ = msg
        return self._config.input_queue

    def _enrich(self, msg: Message, output: dict[str, Any]) -> Message:
        next_reply_queue = _NEXT_REPLY_QUEUE.get(msg.reply_queue, msg.reply_queue)
        metadata = dict(msg.metadata)
        metadata.pop("__loop_attempt", None)
        next_run_id = msg.run_id
        payload = self._queue_payload(output)
        if isinstance(payload, dict):
            output_run_id = payload.get("run_id")
            if isinstance(output_run_id, str) and output_run_id.strip():
                next_run_id = output_run_id
        return Message(
            run_id=next_run_id,
            stage=_QUEUE_TO_STAGE.get(msg.reply_queue, self.name),
            payload=payload,
            reply_queue=next_reply_queue,
            dead_letter_queue=msg.dead_letter_queue,
            attempt=1,
            metadata=metadata,
        )

    def _build_loop_message(self, msg: Message, decision: RoutingDecision, output: dict[str, Any]) -> Message:
        current_loop_attempt = self._current_loop_attempt(msg)
        metadata = dict(decision.metadata)
        metadata["__loop_attempt"] = current_loop_attempt + 1
        return Message(
            run_id=msg.run_id,
            stage=msg.stage,
            payload=self._queue_payload(output),
            reply_queue=msg.reply_queue,
            dead_letter_queue=msg.dead_letter_queue,
            attempt=msg.attempt + 1,
            metadata=metadata,
        )

    def _queue_payload(self, output: Any) -> dict[str, Any]:
        if isinstance(output, BaseModel):
            return output.model_dump(mode="json")
        if isinstance(output, dict):
            return output
        validated = JobApplicationContext.model_validate(output)
        return validated.model_dump(mode="json")

    def _current_loop_attempt(self, msg: Message) -> int:
        value = msg.metadata.get("__loop_attempt") if isinstance(msg.metadata, dict) else None
        if isinstance(value, int) and value >= 1:
            return value
        return msg.attempt

    def _current_execution_attempt(self, msg: Message) -> int:
        value = msg.metadata.get("__exec_attempt") if isinstance(msg.metadata, dict) else None
        if isinstance(value, int) and value >= 1:
            return value
        return msg.attempt

    def _build_exception_retry_message(self, msg: Message, next_attempt: int) -> Message:
        metadata = dict(msg.metadata)
        metadata["__exec_attempt"] = next_attempt
        return Message(
            run_id=msg.run_id,
            stage=msg.stage,
            payload=msg.payload,
            reply_queue=msg.reply_queue,
            dead_letter_queue=msg.dead_letter_queue,
            attempt=next_attempt,
            metadata=metadata,
        )

    def _backoff(self, attempt: int) -> int:
        return min(2 ** attempt, 60)

    async def _execute_inner(self, msg: Message) -> Any | None:
        try:
            return await asyncio.wait_for(
                self._stage.execute(msg),
                timeout=self._config.execute_timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._logger.error(
                "stage execute timeout stage=%s run_id=%s timeout=%ss",
                self.name,
                msg.run_id,
                self._config.execute_timeout_seconds,
            )
            return None
        except Exception:
            self._logger.exception("stage execute raised stage=%s run_id=%s", self.name, msg.run_id)
            return None

    def _write_artifact(self, filename: str, content: str, run_id: str) -> Path:
        run_dir = self._artifacts_root / run_id
        artifact_path = run_dir / self.name / filename
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text(content, encoding="utf-8")
        self._append_stage_index(run_dir, filename)
        self._logger.info("STAGE_ARTIFACT stage=%s path=%s", self.name, artifact_path)
        return artifact_path

    def _append_stage_index(self, run_dir: Path, filename: str) -> None:
        index_path = run_dir / "stage_outputs.md"
        rel_path = Path(self.name) / filename
        with index_path.open("a", encoding="utf-8") as handle:
            handle.write(f"- {self.name}: {rel_path}\\n")

    def _is_apply_dryrun(self, msg: Message) -> bool:
        mode = str(msg.metadata.get("run_mode", "")).strip().lower()
        if mode == "apply-dryrun":
            return True
        return bool(msg.metadata.get("dryrun_stop_after_submit", False))

    def _maybe_export_dryrun_fixture(self, ctx: JobApplicationContext, msg: Message) -> None:
        if not self._is_apply_dryrun(msg):
            return

        fixture_name = _DRYRUN_FIXTURE_BY_STAGE.get(self.name)
        if not fixture_name:
            return

        fixture_dir = self._artifacts_root / ctx.run_id / "fixtures"
        fixture_dir.mkdir(parents=True, exist_ok=True)
        out_path = fixture_dir / fixture_name
        out_path.write_text(
            json.dumps(ctx.model_dump(mode="json"), indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        self._logger.info("DRYRUN_FIXTURE_EXPORT stage=%s path=%s", self.name, out_path)

    @abstractmethod
    async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
        ...

    @abstractmethod
    def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
        ...


class _DefaultPassThroughPolicy(RoutingPolicy):
    def evaluate(self, result: Any, message: Message) -> RoutingDecision:
        _ = message
        if getattr(result, "success", False):
            return RoutingDecision("pass")
        return RoutingDecision("block", reason=str(getattr(result, "error", "stage_failed")))
