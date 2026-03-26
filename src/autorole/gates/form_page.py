from __future__ import annotations

from autorole.context import JobApplicationContext

try:
	from pipeline.gates.base import LoopingGate
	from pipeline.types import GateDecision as Decision, GateResult
except Exception:
	from enum import Enum

	class Decision(Enum):
		PASS = "pass"
		BLOCK = "block"
		LOOP = "loop"

	class GateResult:
		def __init__(self, decision: Decision, target: str | None = None, reason: str = "") -> None:
			self.decision = decision
			self.loop_target = target
			self.reason = reason


	class LoopingGate:
		pass


class FormPageGate(LoopingGate):
	"""Controls form_intelligence <-> form_submission loop decisions."""

	@staticmethod
	def _loop(reason: str, target: str) -> GateResult:
		if hasattr(GateResult, "looping"):
			return GateResult.looping(target=target, reason=reason)
		return GateResult(decision=Decision.LOOP, target=target, reason=reason)

	@staticmethod
	def _pass(reason: str) -> GateResult:
		if hasattr(GateResult, "passing"):
			_ = reason
			return GateResult.passing()
		return GateResult(decision=Decision.PASS, reason=reason)

	@staticmethod
	def _block(reason: str) -> GateResult:
		if hasattr(GateResult, "blocking"):
			return GateResult.blocking(reason=reason)
		return GateResult(decision=Decision.BLOCK, reason=reason)

	def evaluate(self, result: object, message: object) -> GateResult:
		_ = message
		ctx = JobApplicationContext.model_validate(getattr(result, "output", {}))

		if ctx.form_session is None:
			return self._block("form_session is None after form_submission")

		action = ctx.form_session.last_advance_action
		if action == "next_page":
			return self._loop(
				reason=f"page={ctx.form_session.page_index - 1} advancing to next",
				target="form_intelligence",
			)
		if action in ("submit", "done"):
			return self._pass(reason=f"form submitted after {ctx.form_session.page_index} page(s)")
		return self._block(f"unrecognised advance_action={action!r}")
