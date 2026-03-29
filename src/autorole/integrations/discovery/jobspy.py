from __future__ import annotations

import inspect
import logging
from datetime import datetime, timezone
from typing import Any

from autorole.config import SearchFilter
from autorole.context import JobListing
from autorole.integrations.scrapers.base import JobDiscoveryProvider

_DEFAULT_SITES = ["indeed", "linkedin", "zip_recruiter"]


class JobSpyDiscoveryProvider(JobDiscoveryProvider):
	"""Discover job-board listings through python-jobspy."""

	def __init__(
		self,
		sites: list[str] | None = None,
		results_wanted: int = 50,
		hours_old: int = 72,
		country_indeed: str = "usa",
	) -> None:
		self._sites = sites or list(_DEFAULT_SITES)
		self._results_wanted = results_wanted
		self._hours_old = hours_old
		self._country_indeed = country_indeed
		self._log = logging.getLogger(__name__)

	async def search(self, filters: SearchFilter) -> list[JobListing]:
		query = " ".join(part.strip() for part in filters.keywords if part.strip()).strip()
		if not query:
			return []

		scrape_jobs = _import_scrape_jobs()
		listings: list[JobListing] = []
		for site in self._sites:
			kwargs = _build_scrape_jobs_kwargs(
				scrape_jobs,
				sites=[site],
				query=query,
				location=filters.location,
				results_wanted=self._results_wanted,
				hours_old=self._hours_old,
				country_indeed=self._country_indeed,
			)
			try:
				rows = scrape_jobs(**kwargs)
			except Exception as exc:
				self._log.warning("jobspy_site_failed site=%s reason=%s", site, exc)
				continue
			listings.extend(_normalize_rows(rows, excludes=filters.exclude))
		return listings


def _import_scrape_jobs() -> Any:
	try:
		from jobspy import scrape_jobs
	except Exception as exc:  # pragma: no cover - exercised by runtime, not tests
		raise RuntimeError(
			"JobSpy discovery requires python-jobspy and pandas to be installed"
		) from exc
	return scrape_jobs


def _build_scrape_jobs_kwargs(
	scrape_jobs: Any,
	*,
	sites: list[str],
	query: str,
	location: str,
	results_wanted: int,
	hours_old: int,
	country_indeed: str,
) -> dict[str, Any]:
	params = set(inspect.signature(scrape_jobs).parameters)
	kwargs: dict[str, Any] = {
		"site_name": sites,
		"search_term": query,
	}
	if "location" in params:
		kwargs["location"] = location
	if "results_wanted" in params:
		kwargs["results_wanted"] = results_wanted
	if "hours_old" in params:
		kwargs["hours_old"] = hours_old
	if "description_format" in params:
		kwargs["description_format"] = "markdown"
	if "country_indeed" in params:
		kwargs["country_indeed"] = country_indeed
	if "verbose" in params:
		kwargs["verbose"] = 0
	if "linkedin_fetch_description" in params and "linkedin" in sites:
		kwargs["linkedin_fetch_description"] = True
	return kwargs


def _normalize_rows(rows: Any, excludes: list[str]) -> list[JobListing]:
	listings: list[JobListing] = []
	iterrows = getattr(rows, "iterrows", None)
	if iterrows is None:
		return listings

	for _, row in iterrows():
		job_url = _row_value(row, "job_url")
		title = _row_value(row, "title")
		company = _row_value(row, "company")
		if not job_url or not title or not company:
			continue
		if _title_excluded(excludes, title):
			continue

		source_site = (_row_value(row, "site") or "jobspy").lower().replace(" ", "_")
		listings.append(
			JobListing(
				job_url=job_url,
				apply_url=_row_value(row, "job_url_direct") or job_url,
				company_name=company,
				job_id=_extract_job_id(row, job_url),
				job_title=title,
				platform=source_site,
				crawled_at=datetime.now(timezone.utc),
			)
		)
	return listings


def _row_value(row: Any, key: str) -> str:
	value = row.get(key) if hasattr(row, "get") else None
	if value is None:
		return ""
	text = str(value).strip()
	if text.lower() == "nan":
		return ""
	return text


def _extract_job_id(row: Any, job_url: str) -> str:
	job_id = _row_value(row, "id") or _row_value(row, "job_id")
	if job_id:
		return job_id
	if "jk=" in job_url:
		return job_url.split("jk=", 1)[1].split("&", 1)[0]
	parts = [part for part in job_url.split("/") if part]
	for part in reversed(parts):
		digits = "".join(ch for ch in part if ch.isdigit())
		if digits:
			return digits
	return str(abs(hash(job_url)))


def _title_excluded(excludes: list[str], title: str) -> bool:
	title_lower = title.lower()
	return any(term.strip().lower() in title_lower for term in excludes if term.strip())


__all__ = ["JobSpyDiscoveryProvider"]