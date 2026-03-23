from __future__ import annotations

from bs4 import BeautifulSoup

from autorole.config import SearchFilter
from autorole.integrations.scrapers.base import ATSScraper
from autorole.integrations.scrapers.models import ApplicationForm, JobDescription, JobMetadata


class GenericScraper(ATSScraper):
	"""Fallback scraper for unknown ATS providers."""

	async def search_jobs(self, filters: SearchFilter) -> list[JobMetadata]:
		_ = filters
		raise NotImplementedError("GenericScraper does not support search_jobs")

	async def fetch_job_description(self, job_url: str) -> JobDescription:
		if self._page is None:
			raise RuntimeError("GenericScraper.fetch_job_description requires a browser page")
		await self._page.goto(job_url, wait_until="networkidle", timeout=30_000)
		html = await self._page.content()
		soup = BeautifulSoup(html, "html.parser")
		main = soup.find("main") or soup.find("article") or soup.body
		raw_html = str(main) if main is not None else html
		plain_text = BeautifulSoup(raw_html, "html.parser").get_text(separator="\n", strip=True)
		title = (soup.find("h1") or soup.find("title"))
		job_title = title.get_text(" ", strip=True) if title else ""
		return JobDescription(
			job_id="",
			job_title=job_title,
			company_name="",
			raw_html=raw_html,
			plain_text=plain_text,
			qualifications=[],
			responsibilities=[],
			preferred_skills=[],
			culture_signals=[],
		)

	async def fetch_application_form(self, apply_url: str) -> ApplicationForm:
		if self._page is None:
			raise RuntimeError("GenericScraper.fetch_application_form requires a browser page")
		await self._page.goto(apply_url, wait_until="networkidle", timeout=30_000)
		return ApplicationForm(
			job_id="",
			apply_url=apply_url,
			fields=[],
			submit_selector='button[type="submit"], input[type="submit"]',
			form_selector="form",
		)
