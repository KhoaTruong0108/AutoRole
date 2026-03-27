from __future__ import annotations

import json

from autorole.context import JobApplicationContext
from autorole.gates.form_page import FormPageGate
from autorole.queue import FORM_INTEL_Q, Message
from autorole.workers.base import RoutingDecision, StageWorker
from autorole.workers.policies import FormPageRoutingPolicy, PassThroughPolicy


class FormSubmissionWorker(StageWorker):
    name = "form_submission"

    def __init__(self, *args: object, use_form_gate: bool = True, **kwargs: object) -> None:
        policy = FormPageRoutingPolicy(FormPageGate()) if use_form_gate else PassThroughPolicy()
        super().__init__(*args, routing_policy=policy, **kwargs)

    def _loop_queue(self, msg: Message) -> str:
        _ = msg
        return FORM_INTEL_Q

    def _build_loop_message(self, msg: Message, decision: RoutingDecision) -> Message:
        loop_message = super()._build_loop_message(msg, decision)
        loop_message.reply_queue = msg.reply_queue
        return loop_message

    async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = attempt
        session = ctx.form_session
        if session is None:
            return
        self._write_artifact(
            f"page_{session.page_index - 1}_outcomes.json",
            json.dumps([item.model_dump(mode="json") for item in session.all_outcomes], indent=2) + "\n",
            ctx.run_id,
        )
        if ctx.applied is not None and ctx.applied.execution_result is not None:
            self._write_artifact(
                "execution_result.json",
                json.dumps(ctx.applied.execution_result.model_dump(mode="json"), indent=2) + "\n",
                ctx.run_id,
            )

    def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = attempt
        session = ctx.form_session
        if session is None:
            return
        action = session.last_advance_action
        page = session.page_index - 1
        print(f"[ok] form_submission -> page={page} advance={action}")
