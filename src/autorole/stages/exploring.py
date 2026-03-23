from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

from autorole.config import AppConfig, SearchFilter
from autorole.context import JobApplicationContext, JobListing
from autorole.integrations.scrapers import get_scraper
from autorole.integrations.scrapers.base import JobBoardScraper, JobPostingExtractor
from autorole.integrations.scrapers.detection import detect_ats


KNOWN_APPLY_SUBURL_BY_PLATFORM: dict[str, str] = {
	"lever": "/apply",
	"smartrecruiters": "/apply",
	"jobvite": "/apply",
}

KNOWN_APPLY_SUBURL_BY_HOST: dict[str, str] = {
	"jobs.lever.co": "/apply",
	"smartrecruiters.com": "/apply",
	"jobs.jobvite.com": "/apply",
}

try:
	from pipeline.interfaces import Stage
	from pipeline.types import Message, StageResult
except Exception:
	class Stage:
		async def execute(self, message: "Message") -> "StageResult":
			raise NotImplementedError

	class Message:
		def __init__(self, run_id: str, payload: Any, metadata: dict[str, Any] | None = None) -> None:
			self.run_id = run_id
			self.payload = payload
			self.metadata = metadata or {}

	class StageResult:
		def __init__(
			self,
			success: bool,
			output: Any = None,
			error: str | None = None,
			error_type: str | None = None,
		) -> None:
			self.success = success
			self.output = output
			self.error = error
			self.error_type = error_type

		@classmethod
		def ok(cls, output: Any) -> "StageResult":
			return cls(success=True, output=output)

		@classmethod
		def fail(cls, error: str, error_type: str = "") -> "StageResult":
			return cls(success=False, error=error, error_type=error_type)


class ExploringStage(Stage):
	name = "exploring"
	concurrency = 1

	def __init__(
		self,
		config: AppConfig,
		scrapers: dict[str, JobBoardScraper],
		ats_pages: dict[str, Any] | None = None,
	) -> None:
		self._config = config
		self._scrapers = scrapers
		self._ats_pages = ats_pages or {}
		self._log = logging.getLogger(__name__)

	async def execute(self, message: Message) -> StageResult:
		payload = message.payload if isinstance(message.payload, dict) else {}
		search = SearchFilter.model_validate(payload.get("search_config", {}))

		listings: list[JobListing] = []
		for platform in search.platforms:
			scraper = self._scrapers.get(platform)
			if scraper is not None:
				try:
					listings.extend(await scraper.search(search))
				except Exception as exc:
					self._log.warning("scraper_failed", extra={"platform": platform, "error": str(exc)})
				continue

			try:
				listings.extend(await _search_via_ats_registry(platform, search, self._ats_pages.get(platform)))
			except Exception as exc:
				self._log.warning(
					"ats_scraper_failed",
					extra={"platform": platform, "error": str(exc)},
				)

		if not listings:
			return StageResult.fail(
				error="No job listings found across all configured platforms",
				error_type="NoListingsFound",
			)

		contexts = [JobApplicationContext(run_id=_make_run_id(listing), listing=listing) for listing in listings]
		return StageResult.ok(output=contexts)


class ManualUrlExploringStage(Stage):
	"""Manual trigger exploring mode based on one explicit job posting URL."""

	name = "exploring"
	concurrency = 1

	def __init__(
		self,
		config: AppConfig,
		extractor: JobPostingExtractor,
		platform_hint: str | None = None,
	) -> None:
		self._config = config
		self._extractor = extractor
		self._platform_hint = platform_hint

	async def execute(self, message: Message) -> StageResult:
		_ = self._config
		payload = message.payload if isinstance(message.payload, dict) else {}
		job_url = payload.get("job_url")
		if not isinstance(job_url, str) or not job_url.strip():
			return StageResult.fail(
				error="ManualUrlExploringStage requires payload.job_url",
				error_type="MissingJobUrl",
			)

		try:
			listing = await self._extractor.extract(job_url.strip(), platform_hint=self._platform_hint)
			resolved_apply_url = _resolve_apply_url(
				listing.job_url,
				listing.apply_url,
				listing.platform,
			)
			listing = listing.model_copy(update={"apply_url": resolved_apply_url})
		except ValueError as exc:
			return StageResult.fail(error=str(exc), error_type="InvalidJobUrl")
		except Exception as exc:
			return StageResult.fail(error=f"Job URL extraction failed: {exc}", error_type="ExtractionError")

		ctx = JobApplicationContext(run_id=_make_run_id(listing), listing=listing)
		return StageResult.ok(output=[ctx])


def _make_run_id(listing: JobListing) -> str:
	company = listing.company_name.lower().replace(" ", "_")
	return f"{company}_{listing.job_id}"


async def _search_via_ats_registry(platform: str, search: SearchFilter, page: Any | None) -> list[JobListing]:
	seed_url = _platform_seed_url(platform)
	ats_scraper = get_scraper(seed_url, page=page)
	metadata_list = await ats_scraper.search_jobs(search)
	listings: list[JobListing] = []
	for metadata in metadata_list:
		if not metadata.job_url or not metadata.job_id:
			continue
		listing_platform = detect_ats(metadata.job_url)
		if listing_platform == "generic":
			listing_platform = platform
		resolved_apply_url = _resolve_apply_url(
			metadata.job_url,
			metadata.apply_url,
			listing_platform,
		)
		listings.append(
			JobListing(
				job_url=metadata.job_url,
				apply_url=resolved_apply_url,
				company_name=metadata.company_name,
				job_id=metadata.job_id,
				job_title=metadata.job_title,
				platform=listing_platform,
				crawled_at=datetime.now(timezone.utc),
			)
		)
	return listings


def _resolve_apply_url(job_url: str, apply_url: str, platform: str) -> str:
	if apply_url.strip():
		return apply_url.strip()

	parsed = urlparse(job_url)
	host = parsed.netloc.lower()
	platform_key = platform.lower()

	suffix = KNOWN_APPLY_SUBURL_BY_PLATFORM.get(platform_key)
	if not suffix:
		for known_host, known_suffix in KNOWN_APPLY_SUBURL_BY_HOST.items():
			if host == known_host or host.endswith(f".{known_host}"):
				suffix = known_suffix
				break

	if not suffix:
		return job_url

	path = parsed.path or "/"
	if path.endswith(suffix):
		return job_url

	base_path = path.rstrip("/")
	resolved_path = f"{base_path}{suffix}" if base_path else suffix
	return urlunparse(parsed._replace(path=resolved_path))


def _platform_seed_url(platform: str) -> str:
	platform_lower = platform.lower()
	if platform_lower == "lever":
		return "https://jobs.lever.co/"
	if platform_lower == "greenhouse":
		return "https://boards.greenhouse.io/"
	if platform_lower == "linkedin":
		return "https://www.linkedin.com/jobs/"
	if platform_lower == "indeed":
		return "https://www.indeed.com/viewjob"
	if platform_lower == "smartrecruiters":
		return "https://www.smartrecruiters.com/"
	return f"https://{platform_lower}.com/"

