from __future__ import annotations

import pytest

from autorole.config import SearchFilter
from autorole.integrations.discovery import build_discovery_providers
from autorole.integrations.discovery.jobspy import (
	JobSpyDiscoveryProvider,
	_build_scrape_jobs_kwargs,
	_normalize_rows,
)


class _FakeFrame:
	def __init__(self, rows: list[dict[str, object]]) -> None:
		self._rows = rows

	def iterrows(self):
		for idx, row in enumerate(self._rows):
			yield idx, row


async def test_jobspy_provider_returns_empty_without_keywords() -> None:
	provider = JobSpyDiscoveryProvider()

	results = await provider.search(SearchFilter(platforms=["jobspy"], keywords=[], location=""))

	assert results == []


def test_build_discovery_providers_includes_jobspy() -> None:
	providers = build_discovery_providers(["jobspy", "workday"])

	assert "jobspy" in providers
	assert isinstance(providers["jobspy"], JobSpyDiscoveryProvider)


def test_normalize_jobspy_rows_to_listings() -> None:
	rows = _FakeFrame(
		[
			{
				"job_url": "https://www.indeed.com/viewjob?jk=abc123",
				"job_url_direct": "https://company.example/apply/abc123",
				"title": "Backend Engineer",
				"company": "Acme",
				"site": "indeed",
			},
		]
	)

	listings = _normalize_rows(rows, excludes=[])

	assert len(listings) == 1
	listing = listings[0]
	assert listing.job_url == "https://www.indeed.com/viewjob?jk=abc123"
	assert listing.apply_url == "https://company.example/apply/abc123"
	assert listing.company_name == "Acme"
	assert listing.job_title == "Backend Engineer"
	assert listing.job_id == "abc123"
	assert listing.platform == "indeed"


def test_normalize_jobspy_rows_applies_title_excludes() -> None:
	rows = _FakeFrame(
		[
			{
				"job_url": "https://www.indeed.com/viewjob?jk=abc123",
				"title": "Senior Director, Platform",
				"company": "Acme",
				"site": "indeed",
			},
		]
	)

	listings = _normalize_rows(rows, excludes=["director"])

	assert listings == []


def test_build_scrape_jobs_kwargs_filters_unsupported_arguments_for_older_jobspy() -> None:
	def fake_scrape_jobs(
		site_name,
		search_term,
		location="",
		results_wanted=15,
		country_indeed="usa",
	):
		return (site_name, search_term, location, results_wanted, country_indeed)

	kwargs = _build_scrape_jobs_kwargs(
		fake_scrape_jobs,
		sites=["indeed", "linkedin"],
		query="senior software engineer",
		location="US",
		results_wanted=50,
		hours_old=72,
		country_indeed="usa",
	)

	assert kwargs == {
		"site_name": ["indeed", "linkedin"],
		"search_term": "senior software engineer",
		"location": "US",
		"results_wanted": 50,
		"country_indeed": "usa",
	}


@pytest.mark.asyncio
async def test_jobspy_provider_keeps_successful_sites_when_one_site_fails(monkeypatch) -> None:
	def fake_scrape_jobs(**kwargs):
		sites = kwargs["site_name"]
		assert len(sites) == 1
		site = sites[0]
		if site == "indeed":
			raise RuntimeError("bad response status code: 401")
		return _FakeFrame(
			[
				{
					"job_url": f"https://example.com/{site}/123",
					"title": "Backend Engineer",
					"company": "Acme",
					"site": site,
				},
			]
		)

	monkeypatch.setattr("autorole.integrations.discovery.jobspy._import_scrape_jobs", lambda: fake_scrape_jobs)
	provider = JobSpyDiscoveryProvider(sites=["indeed", "linkedin"])

	results = await provider.search(SearchFilter(platforms=["jobspy"], keywords=["backend engineer"], location="US"))

	assert len(results) == 1
	assert results[0].platform == "linkedin"
	assert results[0].job_url == "https://example.com/linkedin/123"