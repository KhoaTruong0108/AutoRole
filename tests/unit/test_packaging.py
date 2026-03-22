from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autorole.context import JobApplicationContext, ScoreReport, TailoredResume
from autorole.integrations.renderer import PandocRenderer
from autorole.stages.packaging import PackagingStage
from tests.conftest import SAMPLE_LISTING

try:
	from pipeline.types import Message
except Exception:
	class Message:  # pragma: no cover - fallback when pipeline package is unavailable
		def __init__(self, run_id: str, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
			self.run_id = run_id
			self.payload = payload
			self.metadata = metadata or {}


class MockRenderer:
	def __init__(self, should_fail: bool = False) -> None:
		self.should_fail = should_fail
		self.calls: list[tuple[Path, Path]] = []

	async def render(self, md_path: Path, pdf_path: Path) -> None:
		self.calls.append((md_path, pdf_path))
		if self.should_fail:
			raise RuntimeError("render error")
		pdf_path.write_bytes(b"%PDF-1.7\n")


def _ctx_with_tailored(md_path: Path) -> JobApplicationContext:
	return JobApplicationContext(
		run_id="acme_123",
		listing=SAMPLE_LISTING,
		score=ScoreReport(
			resume_id="master",
			jd_html="",
			jd_breakdown={},
			overall_score=0.8,
			criteria_scores={},
			matched=[],
			mismatched=[],
			scored_at=datetime.now(timezone.utc),
		),
		tailored=TailoredResume(
			resume_id="res-1",
			parent_resume_id="master",
			tailoring_degree=1,
			file_path=str(md_path),
			diff_summary="{}",
			tailored_at=datetime.now(timezone.utc),
		),
	)


async def test_packaging_creates_pdf_at_correct_path(test_config: Any, tmp_path: Path) -> None:
	md_path = tmp_path / "resume_v1.md"
	md_path.write_text("# Resume\n", encoding="utf-8")
	renderer = MockRenderer()
	stage = PackagingStage(test_config, renderer)

	ctx = _ctx_with_tailored(md_path)
	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.packaged is not None
	assert out_ctx.packaged.pdf_path == str(md_path.with_suffix(".pdf"))
	assert Path(out_ctx.packaged.pdf_path).exists()
	assert len(renderer.calls) == 1


async def test_packaging_fails_on_render_error(test_config: Any, tmp_path: Path) -> None:
	md_path = tmp_path / "resume_v1.md"
	md_path.write_text("# Resume\n", encoding="utf-8")
	renderer = MockRenderer(should_fail=True)
	stage = PackagingStage(test_config, renderer)

	ctx = _ctx_with_tailored(md_path)
	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))

	assert not result.success
	assert result.error_type == "RenderError"


async def test_packaging_fails_when_tailored_is_none(test_config: Any) -> None:
	renderer = MockRenderer()
	stage = PackagingStage(test_config, renderer)
	ctx = JobApplicationContext(run_id="acme_123", listing=SAMPLE_LISTING)

	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))

	assert not result.success
	assert result.error_type == "PreconditionError"


async def test_pandoc_renderer_calls_correct_command(monkeypatch: Any, tmp_path: Path) -> None:
	md_path = tmp_path / "resume.md"
	pdf_path = tmp_path / "resume.pdf"
	md_path.write_text("# Resume\n", encoding="utf-8")

	called: dict[str, Any] = {}

	class FakeProc:
		returncode = 0

		async def communicate(self) -> tuple[bytes, bytes]:
			return b"", b""

	async def fake_create_subprocess_exec(*args: Any, **kwargs: Any) -> FakeProc:
		called["args"] = args
		called["kwargs"] = kwargs
		return FakeProc()

	monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

	renderer = PandocRenderer(pandoc_path="pandoc")
	await renderer.render(md_path, pdf_path)

	assert called["args"][0] == "pandoc"
	assert str(md_path) in called["args"]
	assert str(pdf_path) in called["args"]
	assert "--pdf-engine=xelatex" in called["args"]
