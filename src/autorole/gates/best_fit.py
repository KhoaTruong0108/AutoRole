from __future__ import annotations

from enum import Enum

from autorole.context import JobApplicationContext

try:
	from pipeline.gates.base import LoopingGate
	from pipeline.types import GateDecision, GateResult, Message, StageResult
except Exception:
	class GateDecision(Enum):
		PASS = "pass"
		BLOCK = "block"
		LOOP = "loop"

	class GateResult:
		def __init__(self, decision: GateDecision, target: str | None = None, reason: str = "") -> None:
			self.decision = decision
			self.loop_target = target
			self.reason = reason

		@classmethod
		def passing(cls) -> "GateResult":
			return cls(GateDecision.PASS)

		@classmethod
		def blocking(cls, reason: str) -> "GateResult":
			return cls(GateDecision.BLOCK, reason=reason)

		@classmethod
		def looping(cls, target: str, reason: str = "") -> "GateResult":
			return cls(GateDecision.LOOP, target=target, reason=reason)

	class Message:
		def __init__(self, run_id: str, payload: object, attempt: int = 1, metadata: dict | None = None) -> None:
			self.run_id = run_id
			self.payload = payload
			self.attempt = attempt
			self.metadata = metadata or {}

	class StageResult:
		def __init__(self, success: bool, output: object = None) -> None:
			self.success = success
			self.output = output

		@classmethod
		def ok(cls, output: object) -> "StageResult":
			return cls(success=True, output=output)

	class LoopingGate:
		max_attempts: int

		def _should_block_due_to_attempts(self, message: Message) -> bool:
			return message.attempt >= self.max_attempts


class BestFitGate(LoopingGate):
	"""Controls Tailoring <-> Scoring loop decisions."""

	def __init__(self, max_attempts: int) -> None:
		if max_attempts < 1:
			raise ValueError("max_attempts must be >= 1")
		self.max_attempts = max_attempts
		self.loop_target = "scoring"

	def evaluate(self, result: StageResult, message: Message) -> GateResult:
		ctx = JobApplicationContext.model_validate(result.output)
		if ctx.tailored is None or ctx.score is None:
			return GateResult.blocking(
				reason="BestFitGate: tailored or score is None - cannot evaluate"
			)

		if ctx.tailored.tailoring_degree == 0:
			return GateResult.passing()

		current_score = float(ctx.score.overall_score)
		previous_score = message.metadata.get("last_score_before_tailoring")

		if previous_score is None:
			return GateResult.looping(
				target="scoring",
				reason=f"first_tailoring|baseline={current_score:.4f}",
			)

		previous = float(previous_score)
		if self._should_block_due_to_attempts(message):
			return GateResult.blocking(
				reason=f"Max tailoring attempts ({self.max_attempts}) reached. Final score: {current_score:.3f}"
			)

		if current_score > previous:
			return GateResult.looping(
				target="scoring",
				reason=f"score_improved|{previous:.4f}->{current_score:.4f}",
			)

		direction = "stagnated" if current_score == previous else "regressed"
		return GateResult.blocking(
			reason=f"Score {direction}: {previous:.4f} -> {current_score:.4f}. Blocking run."
		)

