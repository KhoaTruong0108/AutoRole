from __future__ import annotations

from pathlib import Path

import pytest

from autorole_next.integrations.llm_apply import (
	DEFAULT_LLM_APPLY_TIMEOUT_SECONDS,
	LlmApplyRuntimeError,
	_resolve_llm_apply_timeout_seconds,
	_write_output_snapshot,
)


def test_resolve_llm_apply_timeout_seconds_uses_default() -> None:
	assert _resolve_llm_apply_timeout_seconds({}) == DEFAULT_LLM_APPLY_TIMEOUT_SECONDS


def test_resolve_llm_apply_timeout_seconds_accepts_numeric_string() -> None:
	assert _resolve_llm_apply_timeout_seconds({"llm_apply_timeout_seconds": "1800"}) == 1800.0


@pytest.mark.parametrize("raw_value", [0, "0", -1, "-5"])
def test_resolve_llm_apply_timeout_seconds_rejects_non_positive(raw_value: object) -> None:
	with pytest.raises(LlmApplyRuntimeError, match="must be greater than 0"):
		_resolve_llm_apply_timeout_seconds({"llm_apply_timeout_seconds": raw_value})


def test_resolve_llm_apply_timeout_seconds_rejects_invalid_value() -> None:
	with pytest.raises(LlmApplyRuntimeError, match="Invalid llm_apply_timeout_seconds"):
		_resolve_llm_apply_timeout_seconds({"llm_apply_timeout_seconds": "abc"})


def test_write_output_snapshot_includes_header(tmp_path: Path) -> None:
	output_path = tmp_path / "claude-output.txt"
	_write_output_snapshot(output_path, "partial body", header="wrapper failed")
	assert output_path.read_text(encoding="utf-8") == "wrapper failed\n\npartial body\n"