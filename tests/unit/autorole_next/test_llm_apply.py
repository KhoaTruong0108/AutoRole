from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autorole_next.integrations.llm_apply import (
	DEFAULT_LLM_APPLY_TIMEOUT_SECONDS,
	LlmApplyRuntimeError,
	_launch_chrome,
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


def test_launch_chrome_coerces_string_profile_dir(monkeypatch, tmp_path: Path) -> None:
	seen: dict[str, object] = {}

	def fake_free_port(port: int) -> None:
		seen["port"] = port

	def fake_seed_chrome_profile(profile_dir: Path, metadata: dict[str, object]) -> None:
		seen["profile_dir"] = profile_dir
		seen["metadata"] = metadata

	def fake_resolve_chrome_path(metadata: dict[str, object]) -> str:
		return "/bin/echo"

	class FakePopen:
		def __init__(self, command: list[str], **kwargs: object) -> None:
			seen["command"] = command
			seen["kwargs"] = kwargs
			self.pid = 1234

	monkeypatch.setattr("autorole_next.integrations.llm_apply._free_port", fake_free_port)
	monkeypatch.setattr("autorole_next.integrations.llm_apply._seed_chrome_profile", fake_seed_chrome_profile)
	monkeypatch.setattr("autorole_next.integrations.llm_apply._resolve_chrome_path", fake_resolve_chrome_path)
	monkeypatch.setattr(subprocess, "Popen", FakePopen)

	log_path = tmp_path / "chrome.log"
	with log_path.open("wb") as log_stream:
		process = _launch_chrome(
			profile_dir=str(tmp_path / "profile-dir"),
			port=9222,
			metadata={},
			log_stream=log_stream,
		)

	assert process.pid == 1234
	assert seen["port"] == 9222
	assert isinstance(seen["profile_dir"], Path)
	assert Path(seen["command"][0]) == Path("/bin/echo")