from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from autorole.context import JobListing
from autorole.integrations.scrapers import get_scraper
from autorole.integrations.scrapers.base import JobPostingExtractor
from autorole.integrations.scrapers.detection import detect_ats


class GenericJobPostingExtractor(JobPostingExtractor):
	"""Extract a single job listing from a concrete job posting page URL."""

	def __init__(self, page: object) -> None:
		self._page = page

	async def extract(self, job_url: str, platform_hint: str | None = None) -> JobListing:
		parsed = urlparse(job_url)
		if parsed.scheme not in {"http", "https"} or not parsed.netloc:
			raise ValueError(f"Invalid job URL: {job_url}")

		detected_platform = _infer_platform(job_url)
		platform = detected_platform if detected_platform != "custom" else (platform_hint or "custom")
		platform = platform.lower()

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
	ats = detect_ats(job_url)
	if ats != "generic":
		return ats

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

	title = ""
	company = ""

	jsonld_title, jsonld_company = _extract_from_jsonld(soup)
	title = jsonld_title or title
	company = jsonld_company or company

	title = title or _extract_meta_content(soup, "property", "og:title")
	company = company or _extract_meta_content(soup, "property", "og:site_name")

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
			title = title or node.get_text(" ", strip=True)
			break

	for selector in [
		"a.topcard__org-name-link",
		"span.topcard__flavor",
		"div.jobsearch-CompanyInfoWithoutHeaderImage div",
		"div[data-company-name='true']",
		".icl-u-lg-mr--sm.icl-u-xs-mr--xs",
	]:
		node = soup.select_one(selector)
		if node and node.get_text(strip=True):
			company = company or node.get_text(" ", strip=True)
			break

	if (not title or not company) and title:
		title_guess, company_guess = _split_title_company(title)
		title = title or title_guess
		company = company or company_guess

	if (not title or not company):
		title_tag = soup.select_one("title")
		if title_tag and title_tag.get_text(strip=True):
			title_guess, company_guess = _split_title_company(title_tag.get_text(" ", strip=True))
			title = title or title_guess
			company = company or company_guess

	return title, company


def _extract_meta_content(soup: BeautifulSoup, attr: str, key: str) -> str:
	node = soup.find("meta", attrs={attr: key})
	if not node:
		return ""
	content = node.get("content")
	return content.strip() if isinstance(content, str) else ""


def _extract_from_jsonld(soup: BeautifulSoup) -> tuple[str, str]:
	for node in soup.select("script[type='application/ld+json']"):
		raw = node.string or node.get_text()
		if not raw:
			continue
		try:
			parsed = json.loads(raw)
		except Exception:
			continue

		for obj in _iter_jsonld_objects(parsed):
			type_value = obj.get("@type")
			types = [type_value] if isinstance(type_value, str) else type_value
			if not isinstance(types, list):
				continue
			if "JobPosting" not in types:
				continue

			title = str(obj.get("title") or obj.get("name") or "").strip()
			hiring = obj.get("hiringOrganization")
			company = ""
			if isinstance(hiring, dict):
				company = str(hiring.get("name") or "").strip()
			elif isinstance(hiring, str):
				company = hiring.strip()
			if title or company:
				return title, company

	return "", ""


def _iter_jsonld_objects(payload: object) -> list[dict[str, object]]:
	if isinstance(payload, dict):
		items: list[dict[str, object]] = [payload]
		graph = payload.get("@graph")
		if isinstance(graph, list):
			items.extend(item for item in graph if isinstance(item, dict))
		return items
	if isinstance(payload, list):
		return [item for item in payload if isinstance(item, dict)]
	return []


def _split_title_company(text: str) -> tuple[str, str]:
	clean = " ".join(text.split())
	if not clean:
		return "", ""

	lower = clean.lower()
	for sep in [" at ", " @ "]:
		idx = lower.find(sep)
		if idx == -1:
			continue
		title = clean[:idx].strip(" -|:")
		rest = clean[idx + len(sep) :].strip()
		for end_sep in [" | ", " - ", " • "]:
			if end_sep in rest:
				rest = rest.split(end_sep, 1)[0].strip()
		company = rest.strip(" -|:")
		return title, company

	if " - " in clean:
		left, right = clean.split(" - ", 1)
		if left and right and len(left.split()) <= 12:
			return left.strip(), right.split(" | ", 1)[0].strip()

	return clean, ""
