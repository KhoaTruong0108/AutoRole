from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from urllib.parse import urlencode

from autorole.config import SearchFilter
from autorole.context import JobListing
from autorole.integrations.scrapers.base import JobBoardScraper


class LinkedInScraper(JobBoardScraper):
	"""Scrapes job cards from LinkedIn search result pages."""

	BASE_URL = "https://www.linkedin.com/jobs/search/"

	def __init__(self, page: object, jitter_ms: tuple[int, int] = (800, 2000)) -> None:
		self._page = page
		self._jitter = jitter_ms

	async def search(self, filters: SearchFilter) -> list[JobListing]:
		params = _build_linkedin_params(filters)
		listings: list[JobListing] = []

		await self._page.goto(f"{self.BASE_URL}?{urlencode(params)}")
		await self._page.wait_for_selector(".jobs-search-results__list")

		cards = await self._page.query_selector_all(".job-card-container")
		for card in cards:
			listing = await _parse_linkedin_card(card)
			if listing is not None:
				listings.append(listing)
			await asyncio.sleep(random.uniform(*self._jitter) / 1000)

		return listings


def _build_linkedin_params(filters: SearchFilter) -> dict[str, str]:
	params: dict[str, str] = {}
	if filters.keywords:
		params["keywords"] = " ".join(filters.keywords)
	if filters.location:
		params["location"] = filters.location
	if filters.seniority:
		params["f_E"] = ",".join(filters.seniority)
	return params


async def _parse_linkedin_card(card: object) -> JobListing | None:
	title_node = await card.query_selector(".job-card-list__title")
	company_node = await card.query_selector(".job-card-container__company-name")
	link_node = await card.query_selector("a")

	if title_node is None or company_node is None or link_node is None:
		return None

	title = (await title_node.inner_text()).strip()
	company = (await company_node.inner_text()).strip()
	job_url = await link_node.get_attribute("href")
	if not job_url:
		return None

	job_id = _extract_job_id(job_url)
	return JobListing(
		job_url=job_url,
		company_name=company,
		job_id=job_id,
		job_title=title,
		platform="linkedin",
		crawled_at=datetime.now(timezone.utc),
	)


def _extract_job_id(job_url: str) -> str:
	parts = [part for part in job_url.split("/") if part]
	for part in reversed(parts):
		digits = "".join(ch for ch in part if ch.isdigit())
		if digits:
			return digits
	return str(abs(hash(job_url)))


