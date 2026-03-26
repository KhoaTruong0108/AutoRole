from __future__ import annotations

from autorole.integrations.form_controls.executor import FormExecutor
from autorole.integrations.form_controls.models import ExtractedField, FillInstruction


class _SelectLocator:
	def __init__(self, options: list[str]) -> None:
		self.options = options
		self.selected: str | None = None

	@property
	def first(self) -> "_SelectLocator":
		return self

	async def wait_for(self, **_kwargs: object) -> None:
		return None

	async def select_option(self, label: str) -> None:
		if label not in self.options:
			raise ValueError(f"unknown option: {label}")
		self.selected = label


class _SelectPage:
	def __init__(self, options: list[str]) -> None:
		self._locator = _SelectLocator(options)

	def locator(self, _selector: str) -> _SelectLocator:
		return self._locator

	async def wait_for_timeout(self, _ms: int) -> None:
		return None


async def test_select_uses_top_fallback_option_when_suggestion_not_exact() -> None:
	page = _SelectPage(["United States", "Canada"])
	executor = FormExecutor()
	field = ExtractedField(
		id="country",
		run_id="run-1",
		page_index=0,
		page_label="Application",
		field_type="select",
		selector="select[name='country']",
		label="Country",
		required=True,
		options=["United States", "Canada"],
		prefilled_value="",
	)
	inst = FillInstruction(
		field_id="country",
		run_id="run-1",
		action="fill",
		value="USA",
		source="generated",
		page_index=0,
	)

	outcomes = await executor.execute_page(page, [field], [inst])

	assert outcomes[0].status == "ok"
	assert page._locator.selected == "United States"
