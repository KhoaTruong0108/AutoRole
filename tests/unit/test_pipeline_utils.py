from __future__ import annotations

from autorole import __main__ as autorole_main
from autorole import pipeline


def test_inject_loop_metadata_from_gate_reason_success() -> None:
	meta = pipeline.inject_loop_metadata_from_gate_reason({}, "first_tailoring|baseline=0.7200")
	assert meta["last_score_before_tailoring"] == 0.72


def test_inject_loop_metadata_from_gate_reason_invalid() -> None:
	meta = pipeline.inject_loop_metadata_from_gate_reason({"x": 1}, "first_tailoring|baseline=abc")
	assert meta == {"x": 1}


def test_main_entrypoint_calls_cli_app(monkeypatch) -> None:
	called = {"ok": False}

	def fake_app() -> None:
		called["ok"] = True

	monkeypatch.setattr(autorole_main, "app", fake_app)
	autorole_main.main()
	assert called["ok"] is True
