from __future__ import annotations

from types import SimpleNamespace

import pytest

from autorole.integrations.form_controls.extractor import SemanticFieldExtractor
from autorole.integrations.form_controls.extractor import _build_selector, _classify_field_type


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
	def __init__(self, count_value: int) -> None:
		self._count_value = count_value

	async def count(self) -> int:
		return self._count_value

	async def evaluate_handle(self, _script: str) -> _RootHandle:
		return _RootHandle()


class _Page:
	def locator(self, selector: str) -> _Locator:
		if selector == "form#application_form":
			return _Locator(0)
		return _Locator(1)

	async def evaluate(self, _script: str, _root_handle: object) -> list[dict[str, object]]:
		return []


@pytest.mark.asyncio
async def test_extract_missing_root_selector_falls_back_to_body() -> None:
	extractor = SemanticFieldExtractor(_Page())
	section = SimpleNamespace(label="Application", root="form#application_form")

	fields = await extractor.extract(section, run_id="run-1", page_index=0)

	assert fields == []
