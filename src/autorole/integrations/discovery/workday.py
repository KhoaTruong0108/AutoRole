from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources

import httpx

from autorole.config import SearchFilter
from autorole.context import JobListing
from autorole.integrations.scrapers.base import JobDiscoveryProvider

_REMOTE_HINTS = ("remote", "anywhere", "work from home", "wfh", "distributed")
_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"


@dataclass(frozen=True, slots=True)
class WorkdayEmployer:
	name: str
	tenant: str
	site_id: str
	base_url: str

def load_workday_employers() -> dict[str, WorkdayEmployer]:
	config_path = resources.files("autorole.integrations.discovery").joinpath("config/employers.yaml")
	loaded = _load_yaml_data(config_path.read_text(encoding="utf-8"))
	items = loaded.get("employers", {}) if isinstance(loaded, dict) else {}
	employers: dict[str, WorkdayEmployer] = {}
	if not isinstance(items, dict):
		return employers
	for employer_id, item in items.items():
		if not isinstance(employer_id, str) or not isinstance(item, dict):
			continue
		name = str(item.get("name") or "").strip()
		tenant = str(item.get("tenant") or "").strip()
		site_id = str(item.get("site_id") or "").strip()
		base_url = str(item.get("base_url") or "").strip()
		if not name or not tenant or not site_id or not base_url:
			continue
		employers[employer_id] = WorkdayEmployer(
			name=name,
			tenant=tenant,
			site_id=site_id,
			base_url=base_url,
		)
	return employers


def _load_yaml_data(text: str) -> dict[str, object]:
	try:
		import yaml  # type: ignore
	except Exception:
		return _fallback_parse_employers_yaml(text)
	loaded = yaml.safe_load(text) or {}
	if isinstance(loaded, dict):
		return loaded
	return {}


def _fallback_parse_employers_yaml(text: str) -> dict[str, object]:
	employers: dict[str, dict[str, str]] = {}
	in_employers = False
	current_employer_id: str | None = None
	for raw_line in text.splitlines():
		line = raw_line.rstrip()
		stripped = line.strip()
		if not stripped or stripped.startswith("#"):
			continue
		if stripped == "employers:":
			in_employers = True
			continue
		if not in_employers:
			continue
		indent = len(line) - len(line.lstrip(" "))
		if indent == 2 and stripped.endswith(":"):
			current_employer_id = stripped[:-1].strip()
			if current_employer_id:
				employers[current_employer_id] = {}
			continue
		if indent < 4 or current_employer_id is None or ":" not in stripped:
			continue
		key, value = stripped.split(":", 1)
		employers[current_employer_id][key.strip()] = value.strip().strip('"').strip("'")
	return {"employers": employers}


DEFAULT_WORKDAY_EMPLOYERS: dict[str, WorkdayEmployer] = load_workday_employers()


class WorkdayDiscoveryProvider(JobDiscoveryProvider):
	"""Discover Workday-hosted jobs via the public CXS API."""

	def __init__(
		self,
		employers: dict[str, WorkdayEmployer] | None = None,
		request_timeout: float = 20.0,
		page_size: int = 20,
		max_pages: int = 2,
	) -> None:
		self._employers = employers or load_workday_employers()
		self._request_timeout = request_timeout
		self._page_size = page_size
		self._max_pages = max_pages
		self._log = logging.getLogger(__name__)

	async def search(self, filters: SearchFilter) -> list[JobListing]:
		query = " ".join(part.strip() for part in filters.keywords if part.strip()).strip()
		if not query:
			return []

		headers = {
			"Accept": "application/json",
			"Content-Type": "application/json",
			"User-Agent": _USER_AGENT,
		}
		async with httpx.AsyncClient(timeout=self._request_timeout, headers=headers) as client:
			results = await asyncio.gather(
				*(
					self._search_employer(client, employer, query, filters)
					for employer in self._employers.values()
				),
				return_exceptions=True,
			)

		listings: list[JobListing] = []
		for employer, result in zip(self._employers.values(), results, strict=False):
			if isinstance(result, Exception):
				self._log.warning(
					"workday_discovery_failed employer=%s url=%s reason=%s",
					employer.name,
					self._search_url(employer),
					_describe_workday_error(result),
				)
				continue
			listings.extend(result)
		return listings

	async def _search_employer(
		self,
		client: httpx.AsyncClient,
		employer: WorkdayEmployer,
		query: str,
		filters: SearchFilter,
	) -> list[JobListing]:
		listings: list[JobListing] = []
		for page_index in range(self._max_pages):
			offset = page_index * self._page_size
			payload = {
				"appliedFacets": {},
				"limit": self._page_size,
				"offset": offset,
				"searchText": query,
			}
			response = await client.post(self._search_url(employer), json=payload)
			response.raise_for_status()
			data = response.json()
			postings = data.get("jobPostings", [])
			if not postings:
				break

			for posting in postings:
				listing = self._to_listing(employer, posting)
				if listing is None:
					continue
				if not _matches_location(filters.location, posting.get("locationsText", "")):
					continue
				if _title_excluded(filters.exclude, listing.job_title):
					continue
				listings.append(listing)

			if offset + self._page_size >= int(data.get("total", 0) or 0):
				break
		return listings

	def _search_url(self, employer: WorkdayEmployer) -> str:
		return f"{employer.base_url}/wday/cxs/{employer.tenant}/{employer.site_id}/jobs"

	def _to_listing(self, employer: WorkdayEmployer, posting: dict[str, object]) -> JobListing | None:
		external_path = str(posting.get("externalPath") or "").strip()
		title = str(posting.get("title") or "").strip()
		if not external_path or not title:
			return None

		job_url = f"{employer.base_url.rstrip('/')}/{employer.site_id}{external_path}"
		job_id = _extract_job_id(external_path)
		return JobListing(
			job_url=job_url,
			apply_url=job_url,
			company_name=employer.name,
			job_id=job_id,
			job_title=title,
			platform="workday",
			crawled_at=datetime.now(timezone.utc),
		)


def _extract_job_id(external_path: str) -> str:
	parts = [part for part in external_path.split("/") if part]
	if parts:
		return parts[-1]
	return str(abs(hash(external_path)))


def _matches_location(target: str, listing_location: str | object) -> bool:
	if not target.strip():
		return True
	location = str(listing_location or "").lower().strip()
	if not location:
		return True
	if any(token in location for token in _REMOTE_HINTS):
		return True
	needles = [target.lower().strip()]
	needles.extend(part.strip().lower() for part in target.split(",") if part.strip())
	return any(needle and needle in location for needle in needles)


def _title_excluded(excludes: list[str], title: str) -> bool:
	title_lower = title.lower()
	return any(term.strip().lower() in title_lower for term in excludes if term.strip())


def _describe_workday_error(error: Exception) -> str:
	if isinstance(error, httpx.HTTPStatusError):
		response = error.response
		status = response.status_code
		details = _extract_error_body_details(response)
		if details:
			return f"HTTP {status} ({details})"
		return f"HTTP {status}"
	if isinstance(error, httpx.HTTPError):
		return f"{type(error).__name__}: {error}"
	return f"{type(error).__name__}: {error}"


def _extract_error_body_details(response: httpx.Response) -> str:
	content_type = response.headers.get("content-type", "")
	if "json" in content_type.lower():
		try:
			payload = response.json()
		except Exception:
			payload = None
		if isinstance(payload, dict):
			for key in ("errorCode", "message", "httpStatus"):
				value = payload.get(key)
				if value not in (None, ""):
					return json.dumps({key: value}) if key == "httpStatus" else str(value)

	body = response.text.strip()
	if not body:
		return ""
	return body[:160].replace("\n", " ")


__all__ = ["DEFAULT_WORKDAY_EMPLOYERS", "WorkdayDiscoveryProvider", "WorkdayEmployer"]