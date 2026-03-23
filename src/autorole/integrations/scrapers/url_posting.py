from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from autorole.context import JobListing
from autorole.integrations.scrapers import get_scraper
from autorole.integrations.scrapers.base import JobPostingExtractor


class GenericJobPostingExtractor(JobPostingExtractor):
	"""Extract a single job listing from a concrete job posting page URL."""

	def __init__(self, page: object) -> None:
		self._page = page

	async def extract(self, job_url: str, platform_hint: str | None = None) -> JobListing:
		parsed = urlparse(job_url)
		if parsed.scheme not in {"http", "https"} or not parsed.netloc:
			raise ValueError(f"Invalid job URL: {job_url}")

		platform = (platform_hint or _infer_platform(job_url)).lower()

		title = ""
		company = ""

		# Prefer ATS-native JD extraction for stable metadata on platforms like Lever/Greenhouse.
		try:
			scraper = get_scraper(job_url, page=self._page)
			jd = await scraper.fetch_job_description(job_url)
			title = jd.job_title.strip()
			company = jd.company_name.strip()
		except Exception:
			pass

		if not title or not company:
			await self._page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
			html = await self._page.content()
			page_title, page_company = _extract_title_company(html)
			title = title or page_title
			company = company or page_company

		if not title or not company:
			raise ValueError("Could not extract required job title/company from page")

		job_id = _extract_job_id(job_url, platform)
		return JobListing(
			job_url=job_url,
			company_name=company,
			job_id=job_id,
			job_title=title,
			platform=platform,
			crawled_at=datetime.now(timezone.utc),
		)


def _infer_platform(job_url: str) -> str:
	host = (urlparse(job_url).netloc or "").lower()
	if "linkedin." in host:
		return "linkedin"
	if "indeed." in host:
		return "indeed"
	return "custom"


def _extract_job_id(job_url: str, platform: str) -> str:
	parsed = urlparse(job_url)
	query = parse_qs(parsed.query)

	if platform == "linkedin":
		current = query.get("currentJobId")
		if current and current[0]:
			return current[0]
	if platform == "indeed":
		jk = query.get("jk")
		if jk and jk[0]:
			return jk[0]

	parts = [part for part in parsed.path.split("/") if part]
	for part in reversed(parts):
		digits = "".join(ch for ch in part if ch.isdigit())
		if digits:
			return digits

	return str(abs(hash(job_url)))


def _extract_title_company(html: str) -> tuple[str, str]:
	soup = BeautifulSoup(html, "html.parser")

	for selector in [
		"h1.top-card-layout__title",
		"h1.topcard__title",
		"h1.jobsearch-JobInfoHeader-title",
		"h1[data-testid='jobsearch-JobInfoHeader-title']",
		"h1",
		"title",
	]:
		node = soup.select_one(selector)
		if node and node.get_text(strip=True):
			title = node.get_text(" ", strip=True)
			break
	else:
		title = ""

	for selector in [
		"a.topcard__org-name-link",
		"span.topcard__flavor",
		"div.jobsearch-CompanyInfoWithoutHeaderImage div",
		"div[data-company-name='true']",
		".icl-u-lg-mr--sm.icl-u-xs-mr--xs",
	]:
		node = soup.select_one(selector)
		if node and node.get_text(strip=True):
			company = node.get_text(" ", strip=True)
			break
	else:
		company = ""

	return title, company
