from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from autorole.config import SearchFilter
from autorole.context import JobListing
from autorole.integrations.scrapers.models import ApplicationForm, JobDescription, JobMetadata


class JobBoardScraper(ABC):
	@abstractmethod
	async def search(self, filters: SearchFilter) -> list[JobListing]:
		"""Return discovered job listings for the given search filters."""


class JobDiscoveryProvider(ABC):
	@abstractmethod
	async def search(self, filters: SearchFilter) -> list[JobListing]:
		"""Return discovered job listings from provider-backed discovery."""


class ATSScraper(ABC):
	"""ATS-aware scraper contract across exploring, scoring, and form phases."""

	def __init__(self, page: Any | None = None) -> None:
		self._page = page

	@abstractmethod
	async def search_jobs(self, filters: SearchFilter) -> list[JobMetadata]:
		"""Search and return metadata for matching roles."""

	@abstractmethod
	async def fetch_job_description(self, job_url: str) -> JobDescription:
		"""Fetch and parse full job description for one role."""

	@abstractmethod
	async def fetch_application_form(self, apply_url: str) -> ApplicationForm:
		"""Fetch and parse application form fields for one role."""


class JobPostingExtractor(ABC):
	@abstractmethod
	async def extract(self, job_url: str, platform_hint: str | None = None) -> JobListing:
		"""Extract one JobListing from a single job posting URL."""


