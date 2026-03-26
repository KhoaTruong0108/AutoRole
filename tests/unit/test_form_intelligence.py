from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autorole.context import FormSession, JobApplicationContext, PackagedResume
from autorole.integrations.form_controls.models import DetectionResult, ExtractedField, FillInstruction
from autorole.stages.form_intelligence import FormIntelligenceStage
from tests.conftest import SAMPLE_LISTING

try:
	from pipeline.types import Message
except Exception:
	class Message:  # pragma: no cover
		def __init__(self, run_id: str, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
			self.run_id = run_id
			self.payload = payload
			self.metadata = metadata or {}


class MockPage:
	def __init__(self, html: str = "") -> None:
		self._html = html
		self.goto_calls = 0
		self.frames: list[Any] = []
		self.url = ""

	async def goto(self, url: str, **_kwargs: Any) -> None:
		self.goto_calls += 1
		self.url = url

	async def content(self) -> str:
		return self._html


class StubExtractor:
	def __init__(self, fields: list[ExtractedField]) -> None:
		self.fields = fields
		self.calls = 0

	async def extract(self, _section: Any, _run_id: str, _page_index: int) -> list[ExtractedField]:
		self.calls += 1
		return self.fields


class StubMapper:
	def __init__(self, instructions: list[FillInstruction]) -> None:
		self.instructions = instructions
		self.calls = 0

	async def map(self, *args: Any, **kwargs: Any) -> list[FillInstruction]:
		_ = (args, kwargs)
		self.calls += 1
		return self.instructions


def _ctx() -> JobApplicationContext:
	return JobApplicationContext(
		run_id="acme_123",
		listing=SAMPLE_LISTING,
		packaged=PackagedResume(
			resume_id="res-1",
			pdf_path="/tmp/resume.pdf",
			packaged_at=datetime.now(timezone.utc),
		),
	)


def _field() -> ExtractedField:
	return ExtractedField(
		id="field-1",
		run_id="acme_123",
		page_index=0,
		page_label="Application form",
		field_type="text",
		selector="[name='email']",
		label="Email",
		required=True,
		options=[],
		prefilled_value="",
	)


def _instruction() -> FillInstruction:
	return FillInstruction(
		field_id="field-1",
		run_id="acme_123",
		action="fill",
		value="me@example.com",
		source="generated",
		page_index=0,
	)


async def test_form_intelligence_initializes_form_session_on_first_iteration(test_config: Any) -> None:
	base_dir = Path(test_config.base_dir)
	(base_dir / "user_profile.json").write_text("{}", encoding="utf-8")

	page = MockPage(html="<html><body>clean</body></html>")
	page.url = "https://example.com/apply"
	extractor = StubExtractor([_field()])
	mapper = StubMapper([_instruction()])

	stage = FormIntelligenceStage(
		test_config,
		llm_client=object(),
		page=page,
		form_extractor=extractor,
		field_mapper=mapper,
	)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.form_session is not None
	assert out_ctx.form_session.page_index == 0
	assert out_ctx.form_session.detection.platform_id in {"generic", "greenhouse", "workday", "lever", "ashby"}
	assert len(out_ctx.form_session.all_fields) == 1
	assert len(out_ctx.form_session.all_instructions) == 1
	assert page.goto_calls == 1


async def test_form_intelligence_reuses_existing_session_without_navigation(test_config: Any) -> None:
	base_dir = Path(test_config.base_dir)
	(base_dir / "user_profile.json").write_text("{}", encoding="utf-8")

	page = MockPage(html="<html><body>clean</body></html>")
	page.url = "https://example.com/apply"
	extractor = StubExtractor([_field()])
	mapper = StubMapper([_instruction()])

	existing_session = FormSession(
		detection=DetectionResult(
			run_id="acme_123",
			platform_id="generic",
			apply_url="https://example.com/apply",
			used_iframe=False,
			detection_method="fallback",
		),
		page_index=1,
	)
	ctx = _ctx().model_copy(update={"form_session": existing_session})

	stage = FormIntelligenceStage(
		test_config,
		llm_client=object(),
		page=page,
		form_extractor=extractor,
		field_mapper=mapper,
	)

	result = await stage.execute(Message(run_id="acme_123", payload=ctx.model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.form_intelligence is not None
	assert out_ctx.form_intelligence.page_index == 1
	assert page.goto_calls == 0
	assert extractor.calls == 1
	assert mapper.calls == 1


async def test_form_intelligence_fails_when_no_fields_extracted(test_config: Any) -> None:
	base_dir = Path(test_config.base_dir)
	(base_dir / "user_profile.json").write_text("{}", encoding="utf-8")

	page = MockPage(html="<html><body>clean</body></html>")
	extractor = StubExtractor([])
	mapper = StubMapper([])
	stage = FormIntelligenceStage(
		test_config,
		llm_client=object(),
		page=page,
		form_extractor=extractor,
		field_mapper=mapper,
	)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert not result.success
	assert result.error_type == "ExtractionError"


async def test_form_intelligence_fails_when_profile_missing(test_config: Any) -> None:
	page = MockPage(html="<html><body>clean</body></html>")
	extractor = StubExtractor([_field()])
	mapper = StubMapper([_instruction()])
	stage = FormIntelligenceStage(
		test_config,
		llm_client=object(),
		page=page,
		form_extractor=extractor,
		field_mapper=mapper,
	)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert not result.success
	assert result.error_type == "ConfigError"
