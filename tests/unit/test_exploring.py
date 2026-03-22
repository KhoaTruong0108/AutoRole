from __future__ import annotations

from datetime import datetime, timezone

from autorole.config import AppConfig
from autorole.context import JobListing
from autorole.stages.exploring import ExploringStage, ManualUrlExploringStage

try:
	from pipeline.types import Message
except Exception:
	class Message:  # pragma: no cover - fallback only when pipeline package is missing
		def __init__(self, run_id: str, payload: dict) -> None:
			self.run_id = run_id
			self.payload = payload


class MockScraper:
	def __init__(self, listings: list[JobListing]) -> None:
		self._listings = listings

	async def search(self, filters: object) -> list[JobListing]:
		_ = filters
		return self._listings


class MockExtractor:
	def __init__(self, listing: JobListing | None = None, error: Exception | None = None) -> None:
		self._listing = listing
		self._error = error

	async def extract(self, _job_url: str, platform_hint: str | None = None) -> JobListing:
		_ = platform_hint
		if self._error is not None:
			raise self._error
		assert self._listing is not None
		return self._listing


def _listing(company: str, job_id: str) -> JobListing:
	return JobListing(
		job_url=f"https://example.com/jobs/{job_id}",
		company_name=company,
		job_id=job_id,
		job_title="Senior Engineer",
		platform="mock",
		crawled_at=datetime.now(timezone.utc),
	)


async def test_exploring_returns_one_context_per_listing() -> None:
	listings = [_listing("Acme Corp", "1"), _listing("Beta Inc", "2"), _listing("Gamma", "3")]
	stage = ExploringStage(AppConfig(), scrapers={"mock": MockScraper(listings)})
	msg = Message(run_id="seed", payload={"search_config": {"platforms": ["mock"]}})

	result = await stage.execute(msg)

	assert result.success
	assert len(result.output) == 3


async def test_exploring_sets_correct_run_id() -> None:
	listing = _listing("Acme Corp", "456")
	stage = ExploringStage(AppConfig(), scrapers={"mock": MockScraper([listing])})
	msg = Message(run_id="seed", payload={"search_config": {"platforms": ["mock"]}})

	result = await stage.execute(msg)

	assert result.success
	assert result.output[0].run_id == "acme_corp_456"


async def test_exploring_continues_on_one_platform_failure() -> None:
	class BrokenScraper:
		async def search(self, _filters: object) -> list[JobListing]:
			raise RuntimeError("boom")

	listing = _listing("Good Co", "999")
	stage = ExploringStage(
		AppConfig(),
		scrapers={"bad": BrokenScraper(), "good": MockScraper([listing])},
	)
	msg = Message(
		run_id="seed",
		payload={"search_config": {"platforms": ["bad", "good"]}},
	)

	result = await stage.execute(msg)

	assert result.success
	assert len(result.output) == 1
	assert result.output[0].listing.company_name == "Good Co"


async def test_exploring_fails_when_no_listings() -> None:
	stage = ExploringStage(AppConfig(), scrapers={"mock": MockScraper([])})
	msg = Message(run_id="seed", payload={"search_config": {"platforms": ["mock"]}})

	result = await stage.execute(msg)

	assert not result.success
	assert result.error_type == "NoListingsFound"


async def test_manual_url_exploring_returns_single_context() -> None:
	listing = _listing("Acme Corp", "123")
	stage = ManualUrlExploringStage(AppConfig(), extractor=MockExtractor(listing=listing))
	msg = Message(run_id="seed", payload={"job_url": "https://example.com/jobs/123"})

	result = await stage.execute(msg)

	assert result.success
	assert len(result.output) == 1
	assert result.output[0].run_id == "acme_corp_123"


async def test_manual_url_exploring_requires_job_url() -> None:
	stage = ManualUrlExploringStage(AppConfig(), extractor=MockExtractor(listing=_listing("A", "1")))
	msg = Message(run_id="seed", payload={})

	result = await stage.execute(msg)

	assert not result.success
	assert result.error_type == "MissingJobUrl"


async def test_manual_url_exploring_invalid_url_error() -> None:
	stage = ManualUrlExploringStage(
		AppConfig(),
		extractor=MockExtractor(error=ValueError("Invalid job URL: bad")),
	)
	msg = Message(run_id="seed", payload={"job_url": "bad"})

	result = await stage.execute(msg)

	assert not result.success
	assert result.error_type == "InvalidJobUrl"

