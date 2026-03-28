from __future__ import annotations

from types import SimpleNamespace

import pytest

from autorole.integrations.form_controls.extractor import SemanticFieldExtractor
from autorole.integrations.form_controls.extractor import _build_selector, _classify_field_type, _load_lazy_options
from autorole.integrations.form_controls.models import ExtractedField


def test_classify_known_html_types() -> None:
	assert _classify_field_type("textarea", "") == "textarea"
	assert _classify_field_type("select", "") == "select"
	assert _classify_field_type("input", "radio") == "radio"
	assert _classify_field_type("input", "checkbox") == "checkbox"
	assert _classify_field_type("input", "date") == "date"
	assert _classify_field_type("input", "file") == "file"
	assert _classify_field_type("input", "hidden") == "hidden"
	assert _classify_field_type("input", "text") == "text"


def test_classify_aria_role_known() -> None:
	assert _classify_field_type("div", "", aria_role="combobox") == "combobox_search"
	assert _classify_field_type("div", "", aria_role="listbox") == "select"
	assert _classify_field_type("div", "", aria_role="radio") == "radio"
	assert _classify_field_type("div", "", aria_role="checkbox") == "checkbox"
	assert _classify_field_type("div", "", aria_role="switch") == "checkbox"
	assert _classify_field_type("div", "", aria_role="spinbutton") == "text"
	assert _classify_field_type("div", "", aria_role="searchbox") == "text"
	assert _classify_field_type("div", "", aria_role="textbox") == "text"


def test_classify_aria_role_unknown() -> None:
	assert _classify_field_type("div", "", aria_role="grid") == "unknown"


def test_classify_contenteditable() -> None:
	assert _classify_field_type("div", "", contenteditable=True) == "textarea"


def test_build_selector_priority() -> None:
	selector = _build_selector(
		name="email",
		id_="email-id",
		label="Work Email",
		data_automation_id="auto-email",
		data_testid="test-email",
	)
	assert selector == (
		'[data-automation-id="auto-email"], '
		'[data-testid="test-email"], '
		'[aria-label="Work Email"], '
		'[name="email"], '
		'[id="email-id"]'
	)

	selector_no_data = _build_selector(
		name="email",
		id_="email-id",
		label="Work Email",
		data_automation_id="",
		data_testid="",
	)
	assert selector_no_data.startswith('[aria-label="Work Email"]')

	selector_name_id_only = _build_selector(
		name="email",
		id_="email-id",
		label="field_0",
		data_automation_id="",
		data_testid="",
	)
	assert selector_name_id_only == '[name="email"], [id="email-id"]'


class _RootHandle:
	async def dispose(self) -> None:
		return None


class _Locator:
	def __init__(self, count_value: int, *, evaluate_value: object | None = None) -> None:
		self._count_value = count_value
		self._evaluate_value = evaluate_value

	async def count(self) -> int:
		return self._count_value

	async def evaluate_handle(self, _script: str) -> _RootHandle:
		return _RootHandle()

	@property
	def first(self) -> _Locator:
		return self

	async def scroll_into_view_if_needed(self, **_kwargs: object) -> None:
		return None

	async def click(self, **_kwargs: object) -> None:
		return None

	async def press(self, _key: str) -> None:
		return None

	async def evaluate(self, _script: str) -> object:
		return self._evaluate_value

	async def all_text_contents(self) -> list[str]:
		return []

	async def inner_text(self) -> str:
		return ""


class _Page:
	def __init__(self, raw_fields: list[dict[str, object]] | None = None) -> None:
		self._raw_fields = raw_fields or []

	def locator(self, selector: str) -> _Locator:
		if selector == "form#application_form":
			return _Locator(0)
		return _Locator(1)

	async def evaluate(self, _script: str, _root_handle: object) -> list[dict[str, object]]:
		return self._raw_fields


@pytest.mark.asyncio
async def test_extract_missing_root_selector_falls_back_to_body() -> None:
	extractor = SemanticFieldExtractor(_Page())
	section = SimpleNamespace(label="Application", root="form#application_form")

	fields = await extractor.extract(section, run_id="run-1", page_index=0)

	assert fields == []


@pytest.mark.asyncio
async def test_extract_skips_internal_combobox_helper_input() -> None:
	raw_fields = [
		{
			"tag": "input",
			"type": "text",
			"role": "combobox",
			"name": "candidate-location",
			"id": "candidate-location",
			"dataAutomationId": "",
			"dataTestId": "",
			"contentEditable": False,
			"insideCombobox": False,
			"label": "Location (City)*",
			"required": True,
			"options": [],
			"prefilled": "",
			"fromShadow": False,
			"idx": 1,
		},
		{
			"tag": "input",
			"type": "text",
			"role": "",
			"name": "",
			"id": "",
			"dataAutomationId": "",
			"dataTestId": "",
			"contentEditable": False,
			"insideCombobox": True,
			"label": "field_4",
			"required": True,
			"options": [],
			"prefilled": "",
			"fromShadow": False,
			"idx": 4,
		},
	]
	extractor = SemanticFieldExtractor(_Page(raw_fields))
	section = SimpleNamespace(label="Application", root="body")

	fields = await extractor.extract(section, run_id="run-1", page_index=0)

	assert [field.label for field in fields] == ["Location (City)*"]
	assert fields[0].field_type == "combobox_search"


class _PopupLocator(_Locator):
	def __init__(self, selector: str) -> None:
		super().__init__(1, evaluate_value=["popup-menu"])
		self._selector = selector

	async def all_text_contents(self) -> list[str]:
		if self._selector == '[role="option"], [role="listbox"] [role="option"], [role="menuitem"], [role="menu"] [role="menuitem"]':
			return ["Yes", "No", "Yes"]
		return []

	async def inner_text(self) -> str:
		if self._selector == '#popup-menu':
			return "Yes\nNo\nYes"
		return ""


class _Keyboard:
	async def press(self, _key: str) -> None:
		return None


class _PopupPage:
	def __init__(self) -> None:
		self.keyboard = _Keyboard()

	def locator(self, selector: str) -> _PopupLocator:
		return _PopupLocator(selector)

	async def wait_for_selector(self, _selector: str, **_kwargs: object) -> None:
		return None

	async def wait_for_timeout(self, _timeout: int) -> None:
		return None


@pytest.mark.asyncio
async def test_load_lazy_options_reads_menuitems_and_dedupes() -> None:
	field = ExtractedField(
		id="field-1",
		run_id="run-1",
		page_index=0,
		page_label="Application",
		field_type="combobox_search",
		selector='[id="question_1"]',
		label="Question",
		required=True,
	)

	options = await _load_lazy_options(_PopupPage(), field)

	assert options == ["Yes", "No"]


class _LargeUnscopedLocator(_Locator):
	def __init__(self, selector: str) -> None:
		super().__init__(1, evaluate_value=[])
		self._selector = selector


class _LargeUnscopedPopupPage:
	def __init__(self) -> None:
		self.keyboard = _Keyboard()

	def locator(self, selector: str) -> _LargeUnscopedLocator:
		return _LargeUnscopedLocator(selector)

	async def wait_for_selector(self, _selector: str, **_kwargs: object) -> None:
		return None

	async def wait_for_timeout(self, _timeout: int) -> None:
		return None

	async def evaluate(self, _script: str, control_ids: list[str]) -> list[str]:
		assert control_ids == []
		return [f"Option {index}" for index in range(30)]


@pytest.mark.asyncio
async def test_load_lazy_options_rejects_large_unscoped_popup_for_non_country_field() -> None:
	field = ExtractedField(
		id="field-2",
		run_id="run-1",
		page_index=0,
		page_label="Application",
		field_type="combobox_search",
		selector='[id="candidate-location"]',
		label="Location (City)*",
		required=True,
	)

	options = await _load_lazy_options(_LargeUnscopedPopupPage(), field)

	assert options == []
