from __future__ import annotations

import logging
from typing import Any

from autorole.config import AppConfig, SearchFilter
from autorole.context import JobApplicationContext, JobListing
from autorole.integrations.scrapers.base import JobBoardScraper, JobPostingExtractor

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

	def __init__(self, config: AppConfig, scrapers: dict[str, JobBoardScraper]) -> None:
		self._config = config
		self._scrapers = scrapers
		self._log = logging.getLogger(__name__)

	async def execute(self, message: Message) -> StageResult:
		payload = message.payload if isinstance(message.payload, dict) else {}
		search = SearchFilter.model_validate(payload.get("search_config", {}))

		listings: list[JobListing] = []
		for platform in search.platforms:
			scraper = self._scrapers.get(platform)
			if scraper is None:
				continue
			try:
				listings.extend(await scraper.search(search))
			except Exception as exc:
				self._log.warning("scraper_failed", extra={"platform": platform, "error": str(exc)})

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
		except ValueError as exc:
			return StageResult.fail(error=str(exc), error_type="InvalidJobUrl")
		except Exception as exc:
			return StageResult.fail(error=f"Job URL extraction failed: {exc}", error_type="ExtractionError")

		ctx = JobApplicationContext(run_id=_make_run_id(listing), listing=listing)
		return StageResult.ok(output=[ctx])


def _make_run_id(listing: JobListing) -> str:
	company = listing.company_name.lower().replace(" ", "_")
	return f"{company}_{listing.job_id}"

