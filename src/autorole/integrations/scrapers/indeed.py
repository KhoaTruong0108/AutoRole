from __future__ import annotations

import asyncio
import random
from datetime import datetime, timezone
from urllib.parse import urlencode

from autorole.config import SearchFilter
from autorole.context import JobListing
from autorole.integrations.scrapers.base import JobBoardScraper


class IndeedScraper(JobBoardScraper):
	"""Scrapes job cards from Indeed search result pages."""

	BASE_URL = "https://www.indeed.com/jobs"

	def __init__(self, page: object, jitter_ms: tuple[int, int] = (800, 2000)) -> None:
		self._page = page
		self._jitter = jitter_ms

	async def search(self, filters: SearchFilter) -> list[JobListing]:
		params = _build_indeed_params(filters)
		listings: list[JobListing] = []

		await self._page.goto(f"{self.BASE_URL}?{urlencode(params)}")
		await self._page.wait_for_selector("main")

		cards = await self._page.query_selector_all("[data-jk], .job_seen_beacon")
		for card in cards:
			listing = await _parse_indeed_card(card)
			if listing is not None:
				listings.append(listing)
			await asyncio.sleep(random.uniform(*self._jitter) / 1000)

		return listings


def _build_indeed_params(filters: SearchFilter) -> dict[str, str]:
	params: dict[str, str] = {}
	if filters.keywords:
		params["q"] = " ".join(filters.keywords)
	if filters.location:
		params["l"] = filters.location
	return params


async def _parse_indeed_card(card: object) -> JobListing | None:
	title_node = await card.query_selector("h2 a, [data-testid='jobTitle'] a")
	company_node = await card.query_selector("[data-testid='company-name'], .companyName")
	if title_node is None or company_node is None:
		return None

	title = (await title_node.inner_text()).strip()
	company = (await company_node.inner_text()).strip()
	job_url = await title_node.get_attribute("href")
	if not job_url:
		return None
	if job_url.startswith("/"):
		job_url = f"https://www.indeed.com{job_url}"

	job_id = await card.get_attribute("data-jk")
	if not job_id:
		job_id = _extract_job_id(job_url)

	return JobListing(
		job_url=job_url,
		company_name=company,
		job_id=job_id,
		job_title=title,
		platform="indeed",
		crawled_at=datetime.now(timezone.utc),
	)


def _extract_job_id(job_url: str) -> str:
	if "jk=" in job_url:
		return job_url.split("jk=", 1)[1].split("&", 1)[0]
	return str(abs(hash(job_url)))


