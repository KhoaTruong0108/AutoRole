from __future__ import annotations

from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from autorole.config import SearchFilter
from autorole.integrations.scrapers.base import ATSScraper
from autorole.integrations.scrapers.models import ApplicationForm, FormField, JobDescription, JobMetadata


class GreenhouseScraper(ATSScraper):
	"""Greenhouse ATS scraper using public boards API."""

	async def search_jobs(self, filters: SearchFilter) -> list[JobMetadata]:
		results: list[JobMetadata] = []
		for board in _resolve_boards(filters):
			results.extend(await _greenhouse_search_jobs(board, filters))
		return results

	async def fetch_job_description(self, job_url: str) -> JobDescription:
		board_token, job_id = _parse_greenhouse_url(job_url)
		return await _greenhouse_fetch_jd(board_token, job_id)

	async def fetch_application_form(self, apply_url: str) -> ApplicationForm:
		board_token, job_id = _parse_greenhouse_url(apply_url)
		return await _greenhouse_fetch_form(board_token, job_id)


async def _greenhouse_search_jobs(board_token: str, filters: SearchFilter) -> list[JobMetadata]:
	url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs"
	async with httpx.AsyncClient(timeout=20.0) as client:
		resp = await client.get(url, params={"content": "true"})
		resp.raise_for_status()
		data = resp.json()

	results: list[JobMetadata] = []
	for job in data.get("jobs", []) or []:
		title = str(job.get("title", ""))
		if filters.keywords and not any(kw.lower() in title.lower() for kw in filters.keywords):
			continue
		department = ""
		departments = job.get("departments", []) or []
		if departments:
			department = str(departments[0].get("name", ""))
		results.append(
			JobMetadata(
				job_id=str(job.get("id", "")),
				job_title=title,
				company_name=board_token,
				location=str((job.get("location", {}) or {}).get("name", "")),
				employment_type="",
				job_url=str(job.get("absolute_url", "")),
				apply_url=str(job.get("absolute_url", "")),
				posted_at=str(job.get("updated_at", "")),
				department=department,
			)
		)
	return [item for item in results if item.job_id and item.job_url]


async def _greenhouse_fetch_jd(board_token: str, job_id: str) -> JobDescription:
	url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}"
	async with httpx.AsyncClient(timeout=20.0) as client:
		resp = await client.get(url, params={"questions": "true"})
		resp.raise_for_status()
		job = resp.json()

	raw_html = str(job.get("content", ""))
	soup = BeautifulSoup(raw_html, "html.parser")
	plain_text = soup.get_text(separator="\n", strip=True)

	return JobDescription(
		job_id=str(job.get("id", job_id)),
		job_title=str(job.get("title", "")),
		company_name=board_token,
		location=str((job.get("location", {}) or {}).get("name", "")),
		employment_type="",
		raw_html=raw_html,
		plain_text=plain_text,
		qualifications=_extract_bullets_by_heading(soup, ["qualifications", "requirements", "you bring", "you have"]),
		responsibilities=_extract_bullets_by_heading(soup, ["responsibilities", "what you'll do", "you will", "role"]),
		preferred_skills=_extract_bullets_by_heading(soup, ["nice to have", "preferred", "bonus", "plus"]),
		culture_signals=_extract_bullets_by_heading(soup, ["about us", "culture", "values", "benefits"]),
	)


async def _greenhouse_fetch_form(board_token: str, job_id: str) -> ApplicationForm:
	url = f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs/{job_id}"
	async with httpx.AsyncClient(timeout=20.0) as client:
		resp = await client.get(url, params={"questions": "true"})
		resp.raise_for_status()
		job = resp.json()

	fields: list[FormField] = []
	for question in job.get("questions", []) or []:
		label = str(question.get("label", ""))
		required = bool(question.get("required", False))
		for field in question.get("fields", []) or []:
			name = str(field.get("name", ""))
			field_type = _map_greenhouse_type(str(field.get("type", "")))
			options = [str(value.get("label", "")) for value in (field.get("values", []) or []) if value.get("label")]
			value_type = "choice" if options else "value"
			fields.append(
				FormField(
					name=name,
					label=label,
					field_type=field_type,
					required=required,
					options=options,
					map_key=f"direct:{name}:{value_type}",
				)
			)

	apply_url = str(job.get("absolute_url", f"https://boards.greenhouse.io/{board_token}/jobs/{job_id}"))
	return ApplicationForm(
		job_id=job_id,
		apply_url=apply_url,
		fields=fields,
		submit_selector='button[type="submit"]',
		form_selector="form#application_form, form",
	)


def _parse_greenhouse_url(url: str) -> tuple[str, str]:
	parsed = urlparse(url)
	parts = [part for part in parsed.path.split("/") if part]
	if len(parts) >= 3 and parts[-2] == "jobs":
		return parts[-3], parts[-1]
	if len(parts) >= 2 and "greenhouse" in parsed.netloc and parts[0] == "jobs":
		query = parsed.query
		if "for=" in query:
			board = query.split("for=", 1)[1].split("&", 1)[0]
			return board, parts[1]
	raise ValueError(f"Invalid Greenhouse URL: {url}")


def _extract_bullets_by_heading(soup: BeautifulSoup, hints: list[str]) -> list[str]:
	for tag in soup.find_all(["h1", "h2", "h3", "h4", "strong", "b"]):
		text = tag.get_text(strip=True).lower()
		if any(hint in text for hint in hints):
			next_list = tag.find_next_sibling(["ul", "ol"])
			if next_list:
				return [li.get_text(strip=True) for li in next_list.find_all("li") if li.get_text(strip=True)]
	return []


def _map_greenhouse_type(kind: str) -> str:
	mapping = {
		"input_text": "text",
		"input_hidden": "hidden",
		"input_file": "file",
		"textarea": "textarea",
		"multi_value_single_select": "select",
		"multi_value_multi_select": "checkbox",
	}
	return mapping.get(kind, "text")


def _resolve_boards(filters: SearchFilter) -> list[str]:
	# Use domain as board token source in v1 (e.g., ["stripe", "webflow"]).
	return [item.strip() for item in filters.domain if item.strip()]
