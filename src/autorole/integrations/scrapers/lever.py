from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from autorole.config import SearchFilter
from autorole.integrations.scrapers.base import ATSScraper
from autorole.integrations.scrapers.models import ApplicationForm, FormField, JobDescription, JobMetadata

LEVER_API_BASE = "https://api.lever.co/v0/postings"


class LeverScraper(ATSScraper):
	"""Lever ATS scraper (API-first + Playwright form fallback)."""

	async def search_jobs(self, filters: SearchFilter) -> list[JobMetadata]:
		results: list[JobMetadata] = []
		for company in _resolve_companies(filters):
			results.extend(await _lever_search_jobs(company, filters))
		return results

	async def fetch_job_description(self, job_url: str) -> JobDescription:
		company, posting_id = _parse_lever_url(job_url)
		return await _lever_fetch_jd(company, posting_id)

	async def fetch_application_form(self, apply_url: str) -> ApplicationForm:
		if self._page is None:
			raise RuntimeError("LeverScraper.fetch_application_form requires a browser page")

		await self._page.goto(apply_url, wait_until="networkidle", timeout=30_000)
		fields: list[FormField] = []
		for element in await self._page.query_selector_all("input, textarea, select"):
			name = await element.get_attribute("name") or await element.get_attribute("id")
			if not name:
				continue
			tag = (await element.evaluate("el => el.tagName.toLowerCase()")) or "input"
			inp_type = (await element.get_attribute("type") or "text").lower()
			label = await element.get_attribute("aria-label") or name
			required = (await element.get_attribute("required")) is not None
			options: list[str] = []
			if tag == "select":
				options = await element.evaluate(
					"el => Array.from(el.options).map(o => o.textContent?.trim() ?? '').filter(Boolean)"
				)
			value_type = "choice" if options else "value"
			fields.append(
				FormField(
					name=name,
					label=str(label),
					field_type=_normalise_field_type(tag, inp_type),
					required=required,
					options=options,
					map_key=f"direct:{name}:{value_type}",
				)
			)

		job_id = _parse_lever_url(apply_url)[1]
		return ApplicationForm(
			job_id=job_id,
			apply_url=apply_url,
			fields=fields,
			submit_selector=".template-btn-submit, button[type='submit']",
			form_selector="form, .application-form",
		)


async def _lever_search_jobs(company: str, filters: SearchFilter) -> list[JobMetadata]:
	params: dict[str, str | int] = {"mode": "json", "limit": 250}
	if filters.location:
		params["location"] = filters.location

	async with httpx.AsyncClient(timeout=20.0) as client:
		resp = await client.get(f"{LEVER_API_BASE}/{company}", params=params)
		resp.raise_for_status()
		postings = resp.json()

	results: list[JobMetadata] = []
	for posting in postings:
		title = str(posting.get("text", ""))
		if filters.keywords and not any(kw.lower() in title.lower() for kw in filters.keywords):
			continue
		if filters.seniority and not any(level.lower() in title.lower() for level in filters.seniority):
			continue

		cats = posting.get("categories", {}) or {}
		results.append(
			JobMetadata(
				job_id=str(posting.get("id", "")),
				job_title=title,
				company_name=company,
				location=str(cats.get("location", "")),
				employment_type=str(cats.get("commitment", "")),
				job_url=str(posting.get("hostedUrl", "")),
				apply_url=str(posting.get("applyUrl", posting.get("hostedUrl", ""))),
				posted_at=_to_iso_string(posting.get("createdAt")),
				department=str(cats.get("department", "")),
				team=str(cats.get("team", "")),
			)
		)
	return [item for item in results if item.job_id and item.job_url]


async def _lever_fetch_jd(company: str, posting_id: str) -> JobDescription:
	async with httpx.AsyncClient(timeout=20.0) as client:
		resp = await client.get(f"{LEVER_API_BASE}/{company}/{posting_id}")
		resp.raise_for_status()
		posting = resp.json()

	description = str(posting.get("description", ""))
	sections_html = description
	for section in posting.get("lists", []) or []:
		sections_html += f"<h3>{section.get('text', '')}</h3><ul>{section.get('content', '')}</ul>"
	plain_text = _html_to_text(sections_html)

	return JobDescription(
		job_id=str(posting.get("id", posting_id)),
		job_title=str(posting.get("text", "")),
		company_name=company,
		location=str((posting.get("categories", {}) or {}).get("location", "")),
		employment_type=str((posting.get("categories", {}) or {}).get("commitment", "")),
		raw_html=sections_html,
		plain_text=plain_text,
		qualifications=_extract_bullets(posting, ["looking for", "requirements", "qualifications", "you have", "you bring"]),
		responsibilities=_extract_bullets(posting, ["you'll do", "you will", "responsibilities", "day-to-day", "role"]),
		preferred_skills=_extract_bullets(posting, ["nice to have", "preferred", "bonus", "plus"]),
		culture_signals=_extract_bullets(posting, ["about us", "culture", "values", "benefits", "perks"]),
	)


def _parse_lever_url(url: str) -> tuple[str, str]:
	parsed = urlparse(url)
	parts = [part for part in parsed.path.split("/") if part]
	if not parts:
		raise ValueError(f"Invalid Lever URL: {url}")
	if parts[-1] == "apply":
		parts = parts[:-1]
	if len(parts) < 2:
		raise ValueError(f"Invalid Lever URL: {url}")
	return parts[-2], parts[-1]


def _extract_bullets(posting: dict, section_hints: list[str]) -> list[str]:
	for section in posting.get("lists", []) or []:
		heading = str(section.get("text", "")).lower()
		if any(hint in heading for hint in section_hints):
			soup = BeautifulSoup(str(section.get("content", "")), "html.parser")
			return [li.get_text(strip=True) for li in soup.find_all("li") if li.get_text(strip=True)]
	return []


def _html_to_text(html: str) -> str:
	return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)


def _resolve_companies(filters: SearchFilter) -> list[str]:
	# Use domain as the source for company slugs in Lever mode (e.g., ["aircall", "stripe"]).
	return [item.strip() for item in filters.domain if item.strip()]


def _to_iso_string(value: object) -> str:
	if isinstance(value, (int, float)):
		try:
			# Lever timestamps can be epoch milliseconds.
			dt = datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
			return dt.isoformat()
		except Exception:
			return ""
	return str(value) if value else ""


def _normalise_field_type(tag: str, inp_type: str) -> str:
	if tag == "textarea":
		return "textarea"
	if tag == "select":
		return "select"
	if inp_type == "file":
		return "file"
	if inp_type == "hidden":
		return "hidden"
	if inp_type == "radio":
		return "radio"
	if inp_type == "checkbox":
		return "checkbox"
	return "text"
