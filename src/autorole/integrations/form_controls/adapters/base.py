from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel

AdvanceAction = Literal["next_page", "submit", "done"]


class PageSection(BaseModel):
	label: str
	root: str


class AdapterConfig(BaseModel):
	apply_button_selector: str | None = None
	next_button_selector: str = ""
	submit_button_selector: str = ""


class ATSAdapter(ABC):
	platform_id: str
	config: AdapterConfig

	@abstractmethod
	async def setup(self, page: object, frame: object | None) -> None:
		"""Called once before form interaction."""

	@abstractmethod
	async def get_current_page_section(self, page: object) -> PageSection:
		"""Read the currently displayed page section without side effects."""

	@abstractmethod
	async def advance(self, page: object) -> AdvanceAction:
		"""Advance to next step or submit and return resulting action."""

	@abstractmethod
	async def get_file_input(self, page: object) -> object | None:
		"""Return the file input locator when present."""

	@abstractmethod
	async def confirm_success(self, page: object) -> bool:
		"""Confirm final submission success state."""

