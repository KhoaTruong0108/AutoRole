from __future__ import annotations

from importlib import resources
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
import textwrap
from urllib.parse import quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from autorole.config import SearchFilter
from autorole.context import JobListing
from autorole.integrations.llm import LLMClient
from autorole.integrations.scrapers.base import JobDiscoveryProvider
from autorole.integrations.scrapers.detection import detect_ats


@dataclass(frozen=True, slots=True)
class DirectSite:
	name: str
	url: str
	type: str = "search"

DEFAULT_DIRECT_SITES: tuple[DirectSite, ...] | None = None


class SmartExtractDiscoveryProvider(JobDiscoveryProvider):
	"""Discover roles from direct sites using HTTP fetch and JSON-LD extraction."""

	def __init__(
		self,
		sites: tuple[DirectSite, ...] | list[DirectSite] | None = None,
		llm_client: LLMClient | None = None,
		render_html: Callable[[str], Awaitable[str]] | None = None,
		request_timeout: float = 20.0,
	) -> None:
		self._sites = tuple(sites or load_sites())
		self._llm = llm_client
		self._render_html = render_html
		self._request_timeout = request_timeout
		self._log = logging.getLogger(__name__)

	async def search(self, filters: SearchFilter) -> list[JobListing]:
		targets = _build_targets(self._sites, filters)
		if not targets:
			return []

		listings: list[JobListing] = []
		async with httpx.AsyncClient(timeout=self._request_timeout, follow_redirects=True) as client:
			for site_name, url in targets:
				try:
					response = await client.get(url, headers={"User-Agent": _USER_AGENT})
					response.raise_for_status()
					html = response.text
					postings = _extract_job_postings_from_html(html)
					if not postings and self._render_html is not None:
						try:
							rendered_html = await self._render_html(url)
						except Exception:
							rendered_html = ""
						if rendered_html:
							html = rendered_html
							postings = _extract_job_postings_from_html(rendered_html)
					for posting in postings:
						listing = _posting_to_listing(site_name, posting, url)
						if listing is None:
							continue
						if _title_excluded(filters.exclude, listing.job_title):
							continue
						if not _matches_location(filters.location, posting):
							continue
						listings.append(listing)
					if postings or self._llm is None:
						continue
					llm_listings = await _extract_with_llm(self._llm, site_name, url, html, filters)
					if llm_listings:
						listings.extend(llm_listings)
						continue
					listings.extend(await _extract_with_selector_llm(self._llm, site_name, url, html, filters))
				except Exception as exc:
					self._log.warning("smartextract_site_failed site=%s url=%s reason=%s", site_name, url, exc)
					continue
		return _dedupe(listings)


_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
_LLM_HTML_LIMIT = 12000
_LLM_SYSTEM_PROMPT = "You extract job listings from career pages. Return only structured data."
_SELECTOR_HTML_LIMIT = 40000
_SELECTOR_SYSTEM_PROMPT = "You identify repeated job cards in careers-page HTML and return only CSS selectors."
_ALLOWED_SELECTOR_ATTRS = {
	"class",
	"href",
	"id",
	"data-testid",
	"data-id",
	"data-qa",
	"data-role",
	"data-automation",
	"role",
	"aria-label",
	"aria-labelledby",
	"title",
}
_UTILITY_CLASS_RE = re.compile(
	r"^("
	r"[a-z]{1,2}-\d+|"
	r"[a-z]{1,3}-[a-z]{1,3}-\d+|"
	r"col-\d+|"
	r"d-\w+|"
	r"align-\w+|justify-\w+|"
	r"flex-\w+|order-\d+|"
	r"text-\w+|font-\w+|"
	r"bg-\w+|border-\w+|"
	r"rounded-?\w*|shadow-?\w*|"
	r"w-\d+|h-\d+|"
	r"position-\w+|overflow-\w+|"
	r"float-\w+|clearfix|"
	r"visible-\w+|invisible|"
	r"sr-only|"
	r"css-[a-z0-9]+|"
	r"sc-[a-zA-Z]+|"
	r"sc-[a-f0-9]+-\d+"
	r")$"
)


class _LLMDiscoveredJob(BaseModel):
	title: str
	company_name: str = ""
	job_url: str = ""
	location: str = ""


class _LLMDiscoveryResponse(BaseModel):
	jobs: list[_LLMDiscoveredJob] = Field(default_factory=list)


class _LLMSelectorResponse(BaseModel):
	job_card: str = ""
	title: str = ""
	url: str = ""
	location: str | None = None
	salary: str | None = None
	description: str | None = None
	error: str = ""


def load_sites() -> tuple[DirectSite, ...]:
	config_path = resources.files("autorole.integrations.discovery").joinpath("config/sites.yaml")
	text = config_path.read_text(encoding="utf-8")
	data = _load_yaml_data(text)
	items = data.get("sites", [])
	loaded: list[DirectSite] = []
	for item in items:
		if not isinstance(item, dict):
			continue
		name = _as_text(item.get("name"))
		url = _as_text(item.get("url"))
		type_ = _as_text(item.get("type")) or "search"
		if not name or not url:
			continue
		loaded.append(DirectSite(name=name, url=url, type=type_))
	return tuple(loaded)


def _load_yaml_data(text: str) -> dict[str, object]:
	try:
		import yaml  # type: ignore
	except Exception:
		return _fallback_parse_sites_yaml(text)
	loaded = yaml.safe_load(text) or {}
	if isinstance(loaded, dict):
		return loaded
	return {}


def _fallback_parse_sites_yaml(text: str) -> dict[str, object]:
	sites: list[dict[str, str]] = []
	current: dict[str, str] | None = None
	in_sites = False
	for raw_line in textwrap.dedent(text).splitlines():
		line = raw_line.rstrip()
		stripped = line.strip()
		if not stripped or stripped.startswith("#"):
			continue
		indent = len(line) - len(line.lstrip(" "))
		if indent == 0 and stripped == "sites:":
			if current:
				sites.append(current)
				current = None
			in_sites = True
			continue
		if not in_sites:
			continue
		if indent == 0 and stripped.endswith(":") and stripped != "sites:":
			break
		if stripped.startswith("-"):
			if current:
				sites.append(current)
			current = {}
			payload = stripped[1:].strip()
			if payload and ":" in payload:
				key, value = payload.split(":", 1)
				current[key.strip()] = value.strip().strip('"').strip("'")
			continue
		if current is None or ":" not in stripped:
			continue
		key, value = stripped.split(":", 1)
		current[key.strip()] = value.strip().strip('"').strip("'")
	if current:
		sites.append(current)
	return {"sites": sites}


def _build_targets(sites: tuple[DirectSite, ...] | list[DirectSite], filters: SearchFilter) -> list[tuple[str, str]]:
	query = " ".join(part.strip() for part in filters.keywords if part.strip()).strip()
	location = filters.location.strip()
	targets: list[tuple[str, str]] = []
	for site in sites:
		if site.type == "search" and not query:
			continue
		url = site.url.replace("{query_encoded}", quote_plus(query))
		url = url.replace("{location_encoded}", quote_plus(location))
		targets.append((site.name, url))
	return targets


def _extract_job_postings_from_html(html: str) -> list[dict[str, object]]:
	soup = BeautifulSoup(html, "html.parser")
	postings: list[dict[str, object]] = []
	for script in soup.select('script[type="application/ld+json"]'):
		text = script.string or script.get_text() or ""
		text = text.strip()
		if not text:
			continue
		try:
			data = json.loads(text)
		except Exception:
			continue
		for posting in _collect_job_postings(data):
			postings.append(_augment_job_posting_from_dom(posting, script))
	return postings


async def _extract_with_llm(
	llm_client: LLMClient,
	site_name: str,
	url: str,
	html: str,
	filters: SearchFilter,
) -> list[JobListing]:
	clean_html = _clean_html_for_llm(html)
	if not clean_html:
		return []
	query = " ".join(part.strip() for part in filters.keywords if part.strip()).strip()
	user_prompt = (
		f"Site: {site_name}\n"
		f"URL: {url}\n"
		f"Search query: {query or '(none)'}\n"
		"Extract up to 10 real job listings visible on the page. "
		"For each, return title, company_name, job_url, and location. "
		"Use absolute job URLs when possible; otherwise leave job_url empty.\n\n"
		f"HTML:\n{clean_html}"
	)
	try:
		result = await llm_client.call(
			system=_LLM_SYSTEM_PROMPT,
			user=user_prompt,
			response_model=_LLMDiscoveryResponse,
			temperature=0.0,
		)
	except Exception:
		return []
	if not isinstance(result, _LLMDiscoveryResponse):
		return []
	listings: list[JobListing] = []
	for job in result.jobs:
		title = job.title.strip()
		job_url = job.job_url.strip()
		if not title or not job_url:
			continue
		if _title_excluded(filters.exclude, title):
			continue
		if not _matches_location_text(filters.location, job.location):
			continue
		platform = detect_ats(job_url)
		if platform == "generic":
			platform = "smartextract"
		listings.append(
			JobListing(
				job_url=job_url,
				apply_url=job_url,
				company_name=job.company_name.strip() or site_name,
				job_id=_job_id({}, job_url),
				job_title=title,
				platform=platform,
				crawled_at=datetime.now(timezone.utc),
			)
		)
	return listings


def _clean_html_for_llm(html: str) -> str:
	soup = BeautifulSoup(html, "html.parser")
	for tag in soup.find_all(["script", "style", "svg", "noscript", "iframe"]):
		tag.decompose()
	body = soup.find("main") or soup.body or soup
	text = str(body)
	if len(text) > _LLM_HTML_LIMIT:
		text = text[:_LLM_HTML_LIMIT]
	return text


async def _extract_with_selector_llm(
	llm_client: LLMClient,
	site_name: str,
	url: str,
	html: str,
	filters: SearchFilter,
) -> list[JobListing]:
	clean_html = _clean_html_for_selector_llm(html)
	if not clean_html:
		return []
	query = " ".join(part.strip() for part in filters.keywords if part.strip()).strip()
	user_prompt = (
		f"Site: {site_name}\n"
		f"URL: {url}\n"
		f"Search query: {query or '(none)'}\n"
		"Find the repeating HTML block that represents a job listing card and return CSS selectors. "
		"Return only these keys: job_card, title, url, location, salary, description, error. "
		"Use simple selectors. The url selector must resolve to an anchor element or link-containing element. "
		"If the page does not contain repeated job cards, return error='no job cards found'.\n\n"
		f"HTML:\n{clean_html}"
	)
	plan = await _call_selector_llm_plan(llm_client, user_prompt)
	if plan is None:
		return []
	if plan.error.strip() or not plan.job_card.strip() or not plan.title.strip():
		return []

	soup = BeautifulSoup(html, "html.parser")
	try:
		cards = soup.select(plan.job_card)
	except Exception:
		return []
	if not cards:
		return []

	listings: list[JobListing] = []
	for card in cards:
		title = _selector_text(card, plan.title)
		job_url = _selector_href(card, plan.url, url)
		if not title or not job_url:
			continue
		if _title_excluded(filters.exclude, title):
			continue
		location = _selector_text(card, plan.location)
		if not _matches_location_text(filters.location, location):
			continue
		platform = detect_ats(job_url)
		if platform == "generic":
			platform = "smartextract"
		listings.append(
			JobListing(
				job_url=job_url,
				apply_url=job_url,
				company_name=site_name,
				job_id=_job_id({}, job_url),
				job_title=title,
				platform=platform,
				crawled_at=datetime.now(timezone.utc),
			)
		)
	return _dedupe(listings)


async def _call_selector_llm_plan(
	llm_client: LLMClient,
	user_prompt: str,
) -> _LLMSelectorResponse | None:
	try:
		result = await llm_client.call(
			system=_SELECTOR_SYSTEM_PROMPT,
			user=user_prompt,
			response_model=_LLMSelectorResponse,
			temperature=0.0,
		)
		if isinstance(result, _LLMSelectorResponse):
			return result
	except Exception:
		pass
	try:
		raw = await llm_client.call(
			system=_SELECTOR_SYSTEM_PROMPT,
			user=user_prompt,
			response_model=None,
			temperature=0.0,
		)
	except Exception:
		return None
	if not isinstance(raw, str) or not raw.strip():
		return None
	parsed = _extract_json_payload(raw)
	if not parsed:
		return None
	normalized = _normalize_selector_payload(parsed)
	try:
		return _LLMSelectorResponse.model_validate(normalized)
	except Exception:
		return None


def _clean_html_for_selector_llm(html: str) -> str:
	soup = BeautifulSoup(html, "html.parser")
	main = soup.find("main") or soup.find(attrs={"role": "main"}) or soup.body or soup
	section_html = _extract_repeating_job_section_html(main)
	fragment = BeautifulSoup(section_html or str(main), "html.parser")
	for tag in fragment.find_all(["script", "style", "svg", "noscript", "iframe", "meta", "link", "head"]):
		tag.decompose()
	for tag in fragment.find_all(True):
		new_attrs: dict[str, object] = {}
		for attr, value in list((tag.attrs or {}).items()):
			if attr in _ALLOWED_SELECTOR_ATTRS or attr.startswith("data-") or attr.startswith("aria-"):
				if attr == "class":
					classes = value if isinstance(value, list) else str(value).split()
					kept = [candidate for candidate in classes if not _UTILITY_CLASS_RE.match(candidate)]
					if kept:
						new_attrs["class"] = kept
					continue
				new_attrs[attr] = value
		tag.attrs = new_attrs
	for tag in fragment.find_all(True):
		if not tag.get_text(strip=True) and not tag.find("a"):
			tag.decompose()
	text = str(fragment)
	if len(text) > _SELECTOR_HTML_LIMIT:
		text = text[:_SELECTOR_HTML_LIMIT]
	return text


def _extract_repeating_job_section_html(root: object) -> str:
	best_parent = None
	best_score = 0
	for parent in root.find_all(True):
		children = [child for child in parent.find_all(recursive=False) if getattr(child, "name", None)]
		if len(children) < 3:
			continue
		groups: dict[str, list[object]] = {}
		for child in children:
			signature = _stable_child_signature(child)
			groups.setdefault(signature, []).append(child)
		for grouped_children in groups.values():
			if len(grouped_children) < 3:
				continue
			linked = sum(1 for child in grouped_children if child.find("a", href=True) is not None)
			textual = sum(1 for child in grouped_children if len(child.get_text(" ", strip=True)) >= 20)
			score = (len(grouped_children) * 4) + (linked * 3) + textual
			if score > best_score and linked >= 2 and textual >= 2:
				best_parent = parent
				best_score = score
	if best_parent is None:
		return ""
	return str(best_parent)


def _extract_json_payload(text: str) -> dict[str, object]:
	clean = text.strip()
	if "```json" in clean:
		clean = clean.split("```json", 1)[1].split("```", 1)[0]
	elif "```" in clean:
		clean = clean.split("```", 1)[1].split("```", 1)[0]
	start = clean.find("{")
	end = clean.rfind("}")
	if start == -1 or end == -1 or end <= start:
		return {}
	clean = clean[start : end + 1]
	clean = re.sub(r"(?m)//.*$", "", clean)
	try:
		loaded = json.loads(clean)
	except Exception:
		return {}
	return loaded if isinstance(loaded, dict) else {}


def _normalize_selector_payload(payload: dict[str, object]) -> dict[str, object]:
	normalized = dict(payload)
	if not normalized.get("job_card"):
		normalized["job_card"] = normalized.get("container") or normalized.get("card") or normalized.get("cards") or ""
	if not normalized.get("url"):
		normalized["url"] = normalized.get("link") or normalized.get("apply_url") or ""
	return normalized


def _stable_child_signature(node: object) -> str:
	name = getattr(node, "name", "") or "div"
	attrs = getattr(node, "attrs", {}) or {}
	for attr_name in ("data-testid", "data-id", "data-qa", "data-role", "data-automation", "role", "itemprop"):
		value = attrs.get(attr_name)
		if value:
			return f"{name}[{attr_name}={value}]"
	classes = attrs.get("class") or []
	if isinstance(classes, str):
		classes = classes.split()
	stable_classes = [candidate for candidate in classes if not _UTILITY_CLASS_RE.match(candidate)]
	if stable_classes:
		return f"{name}.{'/'.join(stable_classes[:2])}"
	return name


def _selector_text(card: object, selector: str | None) -> str:
	if not selector:
		return ""
	try:
		element = card.select_one(selector)
	except Exception:
		return ""
	if element is None:
		return ""
	return element.get_text(" ", strip=True)


def _selector_href(card: object, selector: str | None, source_url: str) -> str:
	if not selector:
		element = card
	else:
		try:
			element = card.select_one(selector)
		except Exception:
			return ""
		if element is None:
			element = card
	if getattr(element, "name", None) == "a":
		href = element.get("href")
	else:
		anchor = element.find("a", href=True)
		href = anchor.get("href") if anchor is not None else (
			element.get("href")
			or element.get("action")
			or element.get("data-url")
			or element.get("data-href")
		)
	if not href:
		return ""
	return urljoin(source_url, href)


def _collect_job_postings(data: object) -> list[dict[str, object]]:
	if isinstance(data, list):
		items: list[dict[str, object]] = []
		for item in data:
			items.extend(_collect_job_postings(item))
		return items
	if not isinstance(data, dict):
		return []
	if data.get("@type") == "JobPosting":
		return [data]
	if "@graph" in data:
		return _collect_job_postings(data.get("@graph"))
	return []


def _augment_job_posting_from_dom(posting: dict[str, object], script_tag: object) -> dict[str, object]:
	if posting.get("url"):
		return posting
	container = script_tag.find_parent(attrs={"data-url": True}) or script_tag.find_parent(attrs={"data-href": True})
	if container is None:
		return posting
	url = container.get("data-url") or container.get("data-href")
	if not url:
		return posting
	augmented = dict(posting)
	augmented["url"] = str(url)
	identifier = augmented.get("identifier")
	if not identifier and container.get("data-id"):
		augmented["identifier"] = {"value": str(container.get("data-id"))}
	return augmented


def _posting_to_listing(site_name: str, posting: dict[str, object], source_url: str) -> JobListing | None:
	title = _as_text(posting.get("title"))
	job_url = _as_text(posting.get("url")) or source_url
	if not title or not job_url:
		return None
	job_url = urljoin(source_url, job_url)
	company = _company_name(posting) or site_name
	platform = detect_ats(job_url)
	if platform == "generic":
		platform = "smartextract"
	return JobListing(
		job_url=job_url,
		apply_url=job_url,
		company_name=company,
		job_id=_job_id(posting, job_url),
		job_title=title,
		platform=platform,
		crawled_at=datetime.now(timezone.utc),
	)


def _company_name(posting: dict[str, object]) -> str:
	hiring = posting.get("hiringOrganization")
	if isinstance(hiring, dict):
		return _as_text(hiring.get("name"))
	return ""


def _job_id(posting: dict[str, object], job_url: str) -> str:
	identifier = posting.get("identifier")
	if isinstance(identifier, dict):
		value = _as_text(identifier.get("value")) or _as_text(identifier.get("name"))
		if value:
			return value
	parts = [part for part in job_url.split("/") if part]
	for part in reversed(parts):
		digits = "".join(ch for ch in part if ch.isdigit())
		if digits:
			return digits
	return str(abs(hash(job_url)))


def _matches_location(target: str, posting: dict[str, object]) -> bool:
	if not target.strip():
		return True
	location = _posting_location(posting).lower()
	if not location:
		return True
	if "remote" in location:
		return True
	needles = [target.lower().strip()]
	needles.extend(part.strip().lower() for part in target.split(",") if part.strip())
	return any(needle and needle in location for needle in needles)


def _matches_location_text(target: str, location: str) -> bool:
	if not target.strip():
		return True
	value = (location or "").lower().strip()
	if not value:
		return True
	if "remote" in value:
		return True
	needles = [target.lower().strip()]
	needles.extend(part.strip().lower() for part in target.split(",") if part.strip())
	return any(needle and needle in value for needle in needles)


def _posting_location(posting: dict[str, object]) -> str:
	job_location = posting.get("jobLocation")
	if isinstance(job_location, list) and job_location:
		return _posting_location(job_location[0] if isinstance(job_location[0], dict) else {})
	if not isinstance(job_location, dict):
		return ""
	address = job_location.get("address")
	if not isinstance(address, dict):
		return ""
	parts = [
		_as_text(address.get("addressLocality")),
		_as_text(address.get("addressRegion")),
		_as_text(address.get("addressCountry")),
	]
	return ", ".join(part for part in parts if part)


def _title_excluded(excludes: list[str], title: str) -> bool:
	title_lower = title.lower()
	return any(term.strip().lower() in title_lower for term in excludes if term.strip())


def _as_text(value: object) -> str:
	if value is None:
		return ""
	text = str(value).strip()
	return "" if text.lower() == "nan" else text


def _dedupe(listings: list[JobListing]) -> list[JobListing]:
	seen: set[str] = set()
	unique: list[JobListing] = []
	for listing in listings:
		key = (listing.apply_url or listing.job_url).strip().lower()
		if not key or key in seen:
			continue
		seen.add(key)
		unique.append(listing)
	return unique


__all__ = [
	"DEFAULT_DIRECT_SITES",
	"DirectSite",
	"SmartExtractDiscoveryProvider",
	"_build_targets",
	"_clean_html_for_llm",
	"_clean_html_for_selector_llm",
	"_extract_job_postings_from_html",
	"_extract_with_llm",
	"_extract_with_selector_llm",
	"load_sites",
	"_posting_to_listing",
]