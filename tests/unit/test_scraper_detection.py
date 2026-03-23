from __future__ import annotations

from autorole.config import SearchFilter
from autorole.integrations.scrapers import get_scraper, register_scraper
from autorole.integrations.scrapers.base import ATSScraper
from autorole.integrations.scrapers.detection import detect_ats
from autorole.integrations.scrapers.models import ApplicationForm, JobDescription, JobMetadata


class FakeLinkedInScraper(ATSScraper):
	async def search_jobs(self, filters: SearchFilter) -> list[JobMetadata]:
		_ = filters
		return []

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


def test_detect_ats_linkedin() -> None:
	assert detect_ats("https://www.linkedin.com/jobs/view/123") == "linkedin"


def test_detect_ats_indeed() -> None:
	assert detect_ats("https://www.indeed.com/viewjob?jk=abc123") == "indeed"


def test_detect_ats_unknown_returns_generic() -> None:
	assert detect_ats("https://careers.example.com/jobs/1") == "generic"


def test_registry_can_resolve_custom_registered_scraper() -> None:
	register_scraper("linkedin", FakeLinkedInScraper)
	scraper = get_scraper("https://www.linkedin.com/jobs/view/123")
	assert isinstance(scraper, FakeLinkedInScraper)
