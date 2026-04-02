from __future__ import annotations

from datetime import datetime, timezone
import json

from autorole.config import AppConfig
from autorole.config import SearchFilter
from autorole.context import ExplorationSeed
from autorole.context import JobListing
from autorole.integrations.scrapers import register_scraper
from autorole.integrations.scrapers.base import ATSScraper, JobDiscoveryProvider
from autorole.integrations.scrapers.models import ApplicationForm, JobDescription, JobMetadata
from autorole.stages.exploring import ExploringStage, ManualUrlExploringStage, UrlListFileExploringStage

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


class MockDiscoveryProvider(JobDiscoveryProvider):
	def __init__(self, listings: list[JobListing]) -> None:
		self._listings = listings

	async def search(self, filters: SearchFilter) -> list[JobListing]:
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


class StubATSSearchScraper(ATSScraper):
	async def search_jobs(self, filters: SearchFilter) -> list[JobMetadata]:
		_ = filters
		return [
			JobMetadata(
				job_id="1001",
				job_title="Platform Engineer",
				company_name="Robo Corp",
				job_url="https://www.smartrecruiters.com/company/jobs/1001",
				apply_url="https://www.smartrecruiters.com/company/jobs/1001/apply",
			)
		]

	async def fetch_job_description(self, job_url: str) -> JobDescription:
		_ = job_url
		return JobDescription(
			job_id="",
			job_title="",
			company_name="",
			raw_html="",
			plain_text="",
			qualifications=[],
			responsibilities=[],
			preferred_skills=[],
			culture_signals=[],
		)

	async def fetch_application_form(self, apply_url: str) -> ApplicationForm:
		return ApplicationForm(
			job_id="",
			apply_url=apply_url,
			fields=[],
			submit_selector="button[type='submit']",
			form_selector="form",
		)


class StubLeverSearchNoApplyScraper(ATSScraper):
	async def search_jobs(self, filters: SearchFilter) -> list[JobMetadata]:
		_ = filters
		return [
			JobMetadata(
				job_id="2002",
				job_title="Backend Engineer",
				company_name="Acme",
				job_url="https://jobs.lever.co/acme/2002",
				apply_url="",
			)
		]

	async def fetch_job_description(self, job_url: str) -> JobDescription:
		_ = job_url
		return JobDescription(
			job_id="",
			job_title="",
			company_name="",
			raw_html="",
			plain_text="",
			qualifications=[],
			responsibilities=[],
			preferred_skills=[],
			culture_signals=[],
		)

	async def fetch_application_form(self, apply_url: str) -> ApplicationForm:
		return ApplicationForm(
			job_id="",
			apply_url=apply_url,
			fields=[],
			submit_selector="button[type='submit']",
			form_selector="form",
		)


def _listing(company: str, job_id: str) -> JobListing:
	return JobListing(
		job_url=f"https://example.com/jobs/{job_id}",
		company_name=company,
		job_id=job_id,
		job_title="Senior Engineer",
		platform="mock",
		crawled_at=datetime.now(timezone.utc),
	)


async def test_exploring_returns_one_seed_per_listing() -> None:
	listings = [_listing("Acme Corp", "1"), _listing("Beta Inc", "2"), _listing("Gamma", "3")]
	stage = ExploringStage(AppConfig(), scrapers={"mock": MockScraper(listings)})
	msg = Message(run_id="seed", payload={"search_config": {"platforms": ["mock"]}})

	result = await stage.execute(msg)

	assert result.success
	assert len(result.output) == 3
	assert all(ExplorationSeed.model_validate(seed).listing.job_id for seed in result.output)


async def test_exploring_emits_source_name_on_seed() -> None:
	listing = _listing("Acme Corp", "456")
	stage = ExploringStage(AppConfig(), scrapers={"mock": MockScraper([listing])})
	msg = Message(run_id="seed", payload={"search_config": {"platforms": ["mock"]}})

	result = await stage.execute(msg)

	assert result.success
	seed = ExplorationSeed.model_validate(result.output[0])
	assert seed.source_name == "mock"


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
	assert ExplorationSeed.model_validate(result.output[0]).listing.company_name == "Good Co"


async def test_exploring_fails_when_no_listings() -> None:
	stage = ExploringStage(AppConfig(), scrapers={"mock": MockScraper([])})
	msg = Message(run_id="seed", payload={"search_config": {"platforms": ["mock"]}})

	result = await stage.execute(msg)

	assert not result.success
	assert result.error_type == "NoListingsFound"


async def test_manual_url_exploring_returns_single_seed() -> None:
	listing = _listing("Acme Corp", "123")
	stage = ManualUrlExploringStage(AppConfig(), extractor=MockExtractor(listing=listing))
	msg = Message(run_id="seed", payload={"job_url": "https://example.com/jobs/123"})

	result = await stage.execute(msg)

	assert result.success
	assert len(result.output) == 1
	seed = ExplorationSeed.model_validate(result.output[0])
	assert seed.source_metadata["manual_url"] is True


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


async def test_url_list_file_exploring_returns_seed_per_unique_url(tmp_path) -> None:
	listing = _listing("Acme Corp", "123")
	job_urls_file = tmp_path / "job_urls.json"
	job_urls_file.write_text(
		json.dumps(
			{
				"job_urls": [
					"https://example.com/jobs/123",
					{"job_url": "https://example.com/jobs/123", "source_name": "manual-upload"},
				]
			}
		),
		encoding="utf-8",
	)
	stage = UrlListFileExploringStage(AppConfig(), extractor=MockExtractor(listing=listing))
	msg = Message(run_id="seed", payload={"job_urls_file": str(job_urls_file)})

	result = await stage.execute(msg)

	assert result.success
	assert len(result.output) == 1
	seed = ExplorationSeed.model_validate(result.output[0])
	assert seed.source_metadata["manual_url_list"] is True
	assert seed.source_metadata["job_urls_file"] == str(job_urls_file)


async def test_url_list_file_exploring_accepts_json_list_entries(tmp_path) -> None:
	listing = _listing("Acme Corp", "123")
	job_urls_file = tmp_path / "job_urls.json"
	job_urls_file.write_text(json.dumps(["https://example.com/jobs/123"]), encoding="utf-8")
	stage = UrlListFileExploringStage(AppConfig(), extractor=MockExtractor(listing=listing))
	msg = Message(run_id="seed", payload={"job_urls_file": str(job_urls_file)})

	result = await stage.execute(msg)

	assert result.success
	seed = ExplorationSeed.model_validate(result.output[0])
	assert seed.source_name == "url_list_file"


async def test_url_list_file_exploring_requires_file_path() -> None:
	stage = UrlListFileExploringStage(AppConfig(), extractor=MockExtractor(listing=_listing("A", "1")))
	msg = Message(run_id="seed", payload={})

	result = await stage.execute(msg)

	assert not result.success
	assert result.error_type == "MissingJobUrlsFile"


async def test_url_list_file_exploring_rejects_invalid_json(tmp_path) -> None:
	job_urls_file = tmp_path / "job_urls.json"
	job_urls_file.write_text("{not-json}", encoding="utf-8")
	stage = UrlListFileExploringStage(AppConfig(), extractor=MockExtractor(listing=_listing("A", "1")))
	msg = Message(run_id="seed", payload={"job_urls_file": str(job_urls_file)})

	result = await stage.execute(msg)

	assert not result.success
	assert result.error_type == "InvalidJobUrlsFile"


async def test_url_list_file_exploring_fails_when_no_valid_urls_extract(tmp_path) -> None:
	job_urls_file = tmp_path / "job_urls.json"
	job_urls_file.write_text(json.dumps(["https://example.com/jobs/123"]), encoding="utf-8")
	stage = UrlListFileExploringStage(
		AppConfig(),
		extractor=MockExtractor(error=ValueError("Invalid job URL: https://example.com/jobs/123")),
	)
	msg = Message(run_id="seed", payload={"job_urls_file": str(job_urls_file)})

	result = await stage.execute(msg)

	assert not result.success
	assert result.error_type == "NoValidJobUrls"


async def test_exploring_uses_ats_registry_when_platform_scraper_missing() -> None:
	register_scraper("smartrecruiters", StubATSSearchScraper)
	stage = ExploringStage(AppConfig(), scrapers={})
	msg = Message(run_id="seed", payload={"search_config": {"platforms": ["smartrecruiters"]}})

	result = await stage.execute(msg)

	assert result.success
	assert len(result.output) == 1
	seed = ExplorationSeed.model_validate(result.output[0])
	assert seed.listing.platform == "smartrecruiters"
	assert seed.listing.apply_url == "https://www.smartrecruiters.com/company/jobs/1001/apply"


async def test_exploring_resolves_apply_url_when_metadata_missing_it() -> None:
	register_scraper("lever", StubLeverSearchNoApplyScraper)
	stage = ExploringStage(AppConfig(), scrapers={})
	msg = Message(run_id="seed", payload={"search_config": {"platforms": ["lever"]}})

	result = await stage.execute(msg)

	assert result.success
	seed = ExplorationSeed.model_validate(result.output[0])
	assert seed.listing.job_url == "https://jobs.lever.co/acme/2002"
	assert seed.listing.apply_url == "https://jobs.lever.co/acme/2002/apply"


async def test_exploring_uses_discovery_provider_when_registered() -> None:
	listing = JobListing(
		job_url="https://mastercard.wd1.myworkdayjobs.com/CorporateCareers/job/Austin-Texas/Platform-Engineer_JR123",
		apply_url="https://mastercard.wd1.myworkdayjobs.com/CorporateCareers/job/Austin-Texas/Platform-Engineer_JR123",
		company_name="Mastercard",
		job_id="Platform-Engineer_JR123",
		job_title="Platform Engineer",
		platform="workday",
		crawled_at=datetime.now(timezone.utc),
	)
	stage = ExploringStage(
		AppConfig(),
		scrapers={},
		discovery_providers={"workday": MockDiscoveryProvider([listing])},
	)
	msg = Message(run_id="seed", payload={"search_config": {"platforms": ["workday"], "keywords": ["platform engineer"]}})

	result = await stage.execute(msg)

	assert result.success
	assert len(result.output) == 1
	seed = ExplorationSeed.model_validate(result.output[0])
	assert seed.listing.platform == "workday"


async def test_exploring_keeps_cross_source_duplicates_for_downstream_dedupe() -> None:
	listing = _listing("Acme Corp", "456")
	listing = listing.model_copy(update={"apply_url": listing.job_url})
	stage = ExploringStage(
		AppConfig(),
		scrapers={"mock": MockScraper([listing])},
		discovery_providers={"workday": MockDiscoveryProvider([listing])},
	)
	msg = Message(run_id="seed", payload={"search_config": {"platforms": ["mock", "workday"]}})

	result = await stage.execute(msg)

	assert result.success
	assert len(result.output) == 2

