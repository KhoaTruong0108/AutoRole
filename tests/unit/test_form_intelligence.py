from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autorole.context import JobApplicationContext, PackagedResume
from autorole.stages import form_intelligence as mod
from autorole.stages.form_intelligence import (
	CaptchaSolver,
	FormIntelligenceStage,
	QuestionnaireAnswers,
)
from tests.conftest import SAMPLE_LISTING

try:
	from pipeline.types import Message
except Exception:
	class Message:  # pragma: no cover - fallback when pipeline package is unavailable
		def __init__(self, run_id: str, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
			self.run_id = run_id
			self.payload = payload
			self.metadata = metadata or {}


class MockPage:
	def __init__(self, html: str = "") -> None:
		self._html = html
		self.goto_calls = 0

	async def goto(self, _url: str, **_kwargs: Any) -> None:
		self.goto_calls += 1

	async def content(self) -> str:
		return self._html


class MockLLM:
	def __init__(self, response: QuestionnaireAnswers) -> None:
		self.response = response

	async def call(self, **_kwargs: Any) -> QuestionnaireAnswers:
		return self.response


class SequenceCaptchaSolver(CaptchaSolver):
	def __init__(self, sequence: list[bool]) -> None:
		super().__init__(api_key="", service="2captcha")
		self.sequence = sequence
		self.calls = 0

	async def solve(self, page: Any, captcha_type: str) -> bool:
		_ = (page, captcha_type)
		value = self.sequence[self.calls] if self.calls < len(self.sequence) else False
		self.calls += 1
		return value


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


def _raw_form() -> dict[str, Any]:
	return {
		"fields": [
			{"id": "email", "label": "Email", "type": "text", "required": True, "options": [], "value": ""},
			{
				"id": "country",
				"label": "Country",
				"type": "single_choice",
				"required": True,
				"options": ["US", "CA"],
				"value": "",
			},
			{
				"id": "resume_upload",
				"label": "Resume",
				"type": "file_upload",
				"required": False,
				"options": [],
				"value": "",
			},
		]
	}


async def test_form_intelligence_extracts_questionnaire(test_config: Any, monkeypatch: Any) -> None:
	page = MockPage(html="<html><body>no captcha</body></html>")
	stage = FormIntelligenceStage(
		test_config,
		MockLLM(QuestionnaireAnswers(answers=[], unanswered_required=[])),
		page,
	)

	async def fake_extract(_page: Any) -> dict[str, Any]:
		return _raw_form()

	monkeypatch.setattr(mod, "_extract_form_fields", fake_extract)
	monkeypatch.setattr(mod, "_merge_answers", lambda raw, answers: raw)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.form_intelligence is not None
	q = out_ctx.form_intelligence.questionnaire
	assert len(q) == 3
	assert q[0]["map"] == "direct:email:value"
	assert q[1]["map"] == "direct:country:choice"
	assert q[2]["map"] == "direct:resume_upload:value"


async def test_form_intelligence_merges_ai_answers(test_config: Any, monkeypatch: Any) -> None:
	page = MockPage(html="<html><body>clean</body></html>")
	answers = QuestionnaireAnswers(
		answers=[
			{"map": "direct:email:value", "answer": "me@example.com"},
			{"map": "direct:country:choice", "answer": "US"},
		],
		unanswered_required=[],
	)
	stage = FormIntelligenceStage(test_config, MockLLM(answers), page)

	async def fake_extract(_page: Any) -> dict[str, Any]:
		return _raw_form()

	monkeypatch.setattr(mod, "_extract_form_fields", fake_extract)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	filled = out_ctx.form_intelligence.form_json_filled
	fields = {f["id"]: f["value"] for f in filled["fields"]}
	assert fields["email"] == "me@example.com"
	assert fields["country"] == "US"


async def test_form_intelligence_blocks_on_unanswered_required_field(test_config: Any, monkeypatch: Any) -> None:
	page = MockPage(html="<html><body>clean</body></html>")
	answers = QuestionnaireAnswers(answers=[], unanswered_required=["direct:email:value"])
	stage = FormIntelligenceStage(test_config, MockLLM(answers), page)

	async def fake_extract(_page: Any) -> dict[str, Any]:
		return _raw_form()

	monkeypatch.setattr(mod, "_extract_form_fields", fake_extract)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert not result.success
	assert result.error_type == "UnansweredRequiredField"


async def test_form_intelligence_blocks_on_captcha_without_solver(test_config: Any) -> None:
	page = MockPage(html="<html><body>recaptcha challenge</body></html>")
	stage = FormIntelligenceStage(
		test_config,
		MockLLM(QuestionnaireAnswers(answers=[], unanswered_required=[])),
		page,
		captcha_solver=None,
	)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert not result.success
	assert result.error_type == "CaptchaChallenge"


async def test_form_intelligence_retries_captcha_with_solver(test_config: Any, monkeypatch: Any) -> None:
	page = MockPage(html="<html><body>recaptcha challenge</body></html>")
	solver = SequenceCaptchaSolver([True])
	stage = FormIntelligenceStage(
		test_config,
		MockLLM(QuestionnaireAnswers(answers=[], unanswered_required=[])),
		page,
		captcha_solver=solver,
	)

	calls = {"count": 0}

	async def fake_detect(_page: Any) -> str | None:
		calls["count"] += 1
		return "recaptcha_v2" if calls["count"] == 1 else None

	async def fake_extract(_page: Any) -> dict[str, Any]:
		return _raw_form()

	monkeypatch.setattr(mod, "_detect_captcha", fake_detect)
	monkeypatch.setattr(mod, "_extract_form_fields", fake_extract)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert result.success
	assert solver.calls == 1


async def test_form_intelligence_blocks_after_max_captcha_attempts(test_config: Any, monkeypatch: Any) -> None:
	page = MockPage(html="<html><body>recaptcha challenge</body></html>")
	solver = SequenceCaptchaSolver([False, False, False])
	stage = FormIntelligenceStage(
		test_config,
		MockLLM(QuestionnaireAnswers(answers=[], unanswered_required=[])),
		page,
		captcha_solver=solver,
	)

	async def always_captcha(_page: Any) -> str | None:
		return "recaptcha_v2"

	monkeypatch.setattr(mod, "_detect_captcha", always_captcha)

	result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

	assert not result.success
	assert result.error_type == "CaptchaChallenge"


async def test_form_intelligence_fails_when_preconditions_not_met(test_config: Any) -> None:
	page = MockPage(html="<html><body>clean</body></html>")
	stage = FormIntelligenceStage(
		test_config,
		MockLLM(QuestionnaireAnswers(answers=[], unanswered_required=[])),
		page,
	)
	ctx = JobApplicationContext(run_id="acme_123")

	result = await stage.execute(Message(run_id="acme_123", payload=ctx.model_dump()))

	assert not result.success
	assert result.error_type == "PreconditionError"
