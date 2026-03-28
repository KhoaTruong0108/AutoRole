from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from autorole.context import FormIntelligenceResult, FormSession, JobApplicationContext, LLMFieldCompletionResult, PackagedResume
from autorole.integrations.form_controls.exceptions import FillError
from autorole.integrations.form_controls.executor import _capture_failure_bundle, _fill_field_with_fallback, _strategy_typed
from autorole.integrations.form_controls.models import DetectionResult, ExtractedField, FieldOutcome, FillInstruction
from autorole.stages.form_submission import FormSubmissionStage
from tests.conftest import SAMPLE_LISTING


class _BundlePage:
	def __init__(self) -> None:
		self.screenshot_paths: list[str] = []

	async def evaluate(self, _script: str, _arg: str) -> str:
		return "<div>context</div>"

	async def screenshot(self, path: str, full_page: bool = False) -> None:
		_ = full_page
		self.screenshot_paths.append(path)
		Path(path).write_bytes(b"png")


class _GatePage:
	def __init__(self) -> None:
		self.screenshots: list[str] = []

	async def screenshot(self, path: str) -> None:
		self.screenshots.append(path)


class _ComboboxLocator:
	def __init__(self, page: "_ComboboxPage", selector: str) -> None:
		self._page = page
		self._selector = selector

	@property
	def first(self) -> "_ComboboxLocator":
		return self

	async def wait_for(self, **_kwargs: object) -> None:
		return None

	async def click(self, **_kwargs: object) -> None:
		self._page.clicks.append(self._selector)
		if self._selector in self._page.option_selectors:
			self._page.selected_option = self._page.option_texts[self._selector]

	async def fill(self, value: str) -> None:
		self._page.fills.append((self._selector, value))

	async def type(self, value: str, delay: int = 0) -> None:
		_ = delay
		self._page.typed.append((self._selector, value))

	async def press(self, key: str) -> None:
		self._page.presses.append((self._selector, key))

	async def count(self) -> int:
		if self._selector in self._page.option_selectors:
			return 1
		if self._selector == self._page.field_selector:
			return 1
		return 0


class _KeyboardPage:
	async def press(self, _key: str) -> None:
		return None


class _ComboboxPage:
	def __init__(self, field_selector: str, option_selectors: dict[str, str]) -> None:
		self.field_selector = field_selector
		self.option_selectors = option_selectors
		self.option_texts = option_selectors
		self.clicks: list[str] = []
		self.fills: list[tuple[str, str]] = []
		self.typed: list[tuple[str, str]] = []
		self.presses: list[tuple[str, str]] = []
		self.selected_option: str | None = None
		self.keyboard = _KeyboardPage()

	def locator(self, selector: str) -> _ComboboxLocator:
		return _ComboboxLocator(self, selector)

	async def wait_for_selector(self, selector: str, **_kwargs: object) -> None:
		if selector == '[role="option"], [role="menuitem"]' and self.option_selectors:
			return None
		raise RuntimeError("selector not visible")

	async def wait_for_timeout(self, _timeout: int) -> None:
		return None


class _FakeAdapter:
	def __init__(self, action: str = "next_page") -> None:
		self.action = action

	async def get_file_input(self, _page: object) -> None:
		return None

	async def advance(self, _page: object) -> str:
		return self.action

	async def confirm_success(self, _page: object) -> bool:
		return True


class _ExecutorStub:
	def __init__(self, outcomes: list[FieldOutcome]) -> None:
		self.outcomes = outcomes

	async def execute_page(
		self,
		_page: object,
		_fields: list[ExtractedField],
		_instructions: list[FillInstruction],
		run_id: str = "",
	) -> list[FieldOutcome]:
		_ = run_id
		return self.outcomes


def _build_ctx(required: bool) -> JobApplicationContext:
	field = ExtractedField(
		id="field-1",
		run_id="run-1",
		page_index=0,
		page_label="Application",
		field_type="text",
		selector='[name="email"]',
		label="Email",
		required=required,
		options=[],
		prefilled_value="",
	)
	inst = FillInstruction(
		field_id=field.id,
		run_id="run-1",
		action="fill",
		value="user@example.com",
		source="generated",
		page_index=0,
	)
	return JobApplicationContext(
		run_id="run-1",
		listing=SAMPLE_LISTING,
		packaged=PackagedResume(
			resume_id="resume-1",
			pdf_path="/tmp/resume.pdf",
			packaged_at=datetime.now(timezone.utc),
		),
		form_session=FormSession(
			detection=DetectionResult(
				run_id="run-1",
				platform_id="generic",
				apply_url="https://example.com/apply",
				used_iframe=False,
				detection_method="fallback",
			),
			page_index=0,
		),
		form_intelligence=FormIntelligenceResult(
			page_index=0,
			page_label="Application",
			extracted_fields=[field],
			fill_instructions=[inst],
			generated_at=datetime.now(timezone.utc),
		),
		llm_field_completion=LLMFieldCompletionResult(
			page_index=0,
			page_label="Application",
			fill_instructions=[inst],
			generated_at=datetime.now(timezone.utc),
		),
	)


async def test_strategy_ladder_uses_first_success(monkeypatch: pytest.MonkeyPatch) -> None:
	calls: list[str] = []

	async def fail_strategy(_page: object, _field: ExtractedField, _value: str) -> None:
		calls.append("fail")
		raise RuntimeError("nope")

	async def ok_strategy(_page: object, _field: ExtractedField, _value: str) -> None:
		calls.append("ok")
		return None

	monkeypatch.setattr(
		"autorole.integrations.form_controls.executor._FILL_STRATEGIES",
		[("first", fail_strategy), ("second", ok_strategy)],
	)

	field = ExtractedField(
		id="f1",
		run_id="run-1",
		page_index=0,
		page_label="Application",
		field_type="text",
		selector='[name="x"]',
		label="X",
		required=False,
	)
	strategy_name, errors = await _fill_field_with_fallback(object(), field, "value")

	assert strategy_name == "second"
	assert calls == ["fail", "ok"]
	assert len(errors) == 1
	assert errors[0].startswith("first:")


async def test_strategy_ladder_exhausted_raises(monkeypatch: pytest.MonkeyPatch) -> None:
	async def fail_one(_page: object, _field: ExtractedField, _value: str) -> None:
		raise RuntimeError("first failed")

	async def fail_two(_page: object, _field: ExtractedField, _value: str) -> None:
		raise RuntimeError("second failed")

	monkeypatch.setattr(
		"autorole.integrations.form_controls.executor._FILL_STRATEGIES",
		[("first", fail_one), ("second", fail_two)],
	)

	field = ExtractedField(
		id="f1",
		run_id="run-1",
		page_index=0,
		page_label="Application",
		field_type="text",
		selector='[name="x"]',
		label="X",
		required=False,
	)

	with pytest.raises(FillError):
		await _fill_field_with_fallback(object(), field, "value")


async def test_capture_bundle_writes_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.chdir(tmp_path)
	page = _BundlePage()
	field = ExtractedField(
		id="field-42",
		run_id="run-1",
		page_index=0,
		page_label="Application",
		field_type="text",
		selector='[name="email"]',
		label="Email",
		required=True,
	)
	instruction = FillInstruction(
		field_id="field-42",
		run_id="run-1",
		action="fill",
		value="x@example.com",
		source="generated",
		page_index=0,
	)

	bundle_path = await _capture_failure_bundle(
		page,
		field,
		instruction,
		["typed: failed"],
		"run-1",
	)
	bundle_dir = Path(bundle_path)

	assert bundle_dir.exists()
	assert (bundle_dir / "field.json").exists()
	assert (bundle_dir / "context.html").exists()
	assert (bundle_dir / "screenshot.png").exists()


async def test_required_field_gate_blocks_required_failures(
	test_config: Any,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	stage = FormSubmissionStage(
		test_config,
		_GatePage(),
		executor=_ExecutorStub(
			[
				FieldOutcome(
					field_id="field-1",
					action_taken="fill",
					value_used="user@example.com",
					status="fill_error",
					error_message="boom",
				)
			]
		),
	)
	monkeypatch.setattr("autorole.stages.form_submission.get_adapter", lambda _platform: _FakeAdapter())

	msg = type("Msg", (), {"payload": _build_ctx(required=True).model_dump(), "metadata": {}, "run_id": "run-1"})
	result = await stage.execute(msg)

	assert not result.success
	assert result.error_type == "RequiredFieldFillError"


async def test_required_field_gate_allows_optional_failures(
	test_config: Any,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	stage = FormSubmissionStage(
		test_config,
		_GatePage(),
		executor=_ExecutorStub(
			[
				FieldOutcome(
					field_id="field-1",
					action_taken="fill",
					value_used="user@example.com",
					status="fill_error",
					error_message="boom",
				)
			]
		),
	)
	monkeypatch.setattr("autorole.stages.form_submission.get_adapter", lambda _platform: _FakeAdapter())

	msg = type("Msg", (), {"payload": _build_ctx(required=False).model_dump(), "metadata": {}, "run_id": "run-1"})
	result = await stage.execute(msg)

	assert result.success


async def test_strategy_typed_combobox_search_clicks_top_menuitem_option() -> None:
	field = ExtractedField(
		id="field-1",
		run_id="run-1",
		page_index=0,
		page_label="Application",
		field_type="combobox_search",
		selector='[id="candidate-location"]',
		label="Location (City)*",
		required=True,
	)
	page = _ComboboxPage(
		field.selector,
		{
			'[role="option"], [role="menuitem"]': "Mountain View, California, United States",
		},
	)

	await _strategy_typed(page, field, "Mountain View")

	assert page.typed == [(field.selector, "Mountain View")]
	assert '[role="option"], [role="menuitem"]' in page.clicks
	assert page.selected_option == "Mountain View, California, United States"


async def test_strategy_typed_combobox_lazy_clicks_exact_option_after_typing() -> None:
	field = ExtractedField(
		id="field-2",
		run_id="run-1",
		page_index=0,
		page_label="Application",
		field_type="combobox_lazy",
		selector='[id="question_61878062"]',
		label="Have you previously applied?",
		required=True,
		options=["Yes", "No"],
	)
	page = _ComboboxPage(
		field.selector,
		{
			'[role="option"]:text-is("Yes"), [role="menuitem"]:text-is("Yes")': "Yes",
			'[role="option"], [role="menuitem"]': "Yes",
		},
	)

	await _strategy_typed(page, field, "Yes")

	assert page.typed == [(field.selector, "Yes")]
	assert '[role="option"]:text-is("Yes"), [role="menuitem"]:text-is("Yes")' in page.clicks
	assert page.selected_option == "Yes"
