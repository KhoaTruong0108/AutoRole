from __future__ import annotations

from typing import Any

from autorole.gates.best_fit import BestFitGate
from autorole.gates.form_page import FormPageGate
from autorole.queue import Message
from autorole.workers.base import RoutingDecision, RoutingPolicy


class PassThroughPolicy(RoutingPolicy):
    def evaluate(self, result: Any, message: Message) -> RoutingDecision:
        _ = message
        if getattr(result, "success", False):
            return RoutingDecision("pass")
        return RoutingDecision("block", reason=str(getattr(result, "error", "stage_failed")))


class BestFitRoutingPolicy(RoutingPolicy):
    def __init__(self, gate: BestFitGate) -> None:
        self._gate = gate

    def evaluate(self, result: Any, message: Message) -> RoutingDecision:
        if not getattr(result, "success", False):
            return RoutingDecision("block", reason=str(getattr(result, "error", "stage_failed")))

        gate_result = self._gate.evaluate(result, message)
        decision = getattr(getattr(gate_result, "decision", None), "value", str(getattr(gate_result, "decision", "")))
        reason = str(getattr(gate_result, "reason", ""))

        if decision == "pass":
            return RoutingDecision("pass")
        if decision == "loop":
            metadata = _inject_loop_metadata(message.metadata, reason)
            return RoutingDecision("loop", reason=reason, metadata=metadata)
        return RoutingDecision("block", reason=reason)


class FormPageRoutingPolicy(RoutingPolicy):
    def __init__(self, gate: FormPageGate) -> None:
        self._gate = gate

    def evaluate(self, result: Any, message: Message) -> RoutingDecision:
        if not getattr(result, "success", False):
            return RoutingDecision("block", reason=str(getattr(result, "error", "stage_failed")))

        gate_result = self._gate.evaluate(result, message)
        decision = getattr(getattr(gate_result, "decision", None), "value", str(getattr(gate_result, "decision", "")))
        reason = str(getattr(gate_result, "reason", ""))

        if decision == "pass":
            return RoutingDecision("pass")
        if decision == "loop":
            return RoutingDecision("loop", reason=reason, metadata=dict(message.metadata))
        return RoutingDecision("block", reason=reason)


def _inject_loop_metadata(metadata: dict[str, Any] | None, reason: str) -> dict[str, Any]:
    base = dict(metadata or {})
    prefix = "first_tailoring|baseline="
    if prefix not in reason:
        return base
    try:
        baseline = float(reason.split(prefix, 1)[1].strip())
    except Exception:
        return base
    base["last_score_before_tailoring"] = baseline
    return base
