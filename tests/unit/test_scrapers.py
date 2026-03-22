from __future__ import annotations

from typing import Any

import pytest

from autorole.config import SearchFilter
from autorole.integrations.scrapers.indeed import (
	_build_indeed_params,
	_extract_job_id as extract_indeed_id,
	_parse_indeed_card,
)
from autorole.integrations.scrapers.linkedin import (
	_build_linkedin_params,
	_extract_job_id as extract_linkedin_id,
	_parse_linkedin_card,
)


class DummyNode:
	def __init__(self, text: str = "", attrs: dict[str, str] | None = None) -> None:
		self._text = text
		self._attrs = attrs or {}

	async def inner_text(self) -> str:
		return self._text

	async def get_attribute(self, name: str) -> str | None:
		return self._attrs.get(name)


class DummyCard:
	def __init__(self, mapping: dict[str, Any], attrs: dict[str, str] | None = None) -> None:
		self.mapping = mapping
		self._attrs = attrs or {}

	async def query_selector(self, selector: str) -> Any:
		return self.mapping.get(selector)

	async def get_attribute(self, name: str) -> str | None:
		return self._attrs.get(name)


def test_build_linkedin_params() -> None:
	filters = SearchFilter(keywords=["python", "backend"], location="Remote", seniority=["4", "5"])
	params = _build_linkedin_params(filters)
	assert params["keywords"] == "python backend"
	assert params["location"] == "Remote"
	assert params["f_E"] == "4,5"


def test_build_indeed_params() -> None:
	filters = SearchFilter(keywords=["platform"], location="SF")
	params = _build_indeed_params(filters)
	assert params == {"q": "platform", "l": "SF"}


def test_extract_job_id_helpers() -> None:
	assert extract_linkedin_id("https://www.linkedin.com/jobs/view/123456789") == "123456789"
	assert extract_indeed_id("https://www.indeed.com/viewjob?jk=abc123&from=serp") == "abc123"


@pytest.mark.asyncio
async def test_parse_linkedin_card_success() -> None:
	card = DummyCard(
		{
			".job-card-list__title": DummyNode("Senior Engineer"),
			".job-card-container__company-name": DummyNode("Acme"),
			"a": DummyNode(attrs={"href": "https://www.linkedin.com/jobs/view/98765"}),
		}
	)
	listing = await _parse_linkedin_card(card)
	assert listing is not None
	assert listing.company_name == "Acme"
	assert listing.job_id == "98765"


@pytest.mark.asyncio
async def test_parse_indeed_card_success() -> None:
	card = DummyCard(
		{
			"h2 a, [data-testid='jobTitle'] a": DummyNode(
				"Backend Engineer", attrs={"href": "/viewjob?jk=abc123"}
			),
			"[data-testid='company-name'], .companyName": DummyNode("Acme"),
		},
		attrs={"data-jk": "abc123"},
	)
	listing = await _parse_indeed_card(card)
	assert listing is not None
	assert listing.job_url.startswith("https://www.indeed.com")
	assert listing.job_id == "abc123"
