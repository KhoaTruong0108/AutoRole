from __future__ import annotations

from abc import ABC, abstractmethod

from autorole.config import SearchFilter
from autorole.context import JobListing


class JobBoardScraper(ABC):
	@abstractmethod
	async def search(self, filters: SearchFilter) -> list[JobListing]:
		"""Return discovered job listings for the given search filters."""


class JobPostingExtractor(ABC):
	@abstractmethod
	async def extract(self, job_url: str, platform_hint: str | None = None) -> JobListing:
		"""Extract one JobListing from a single job posting URL."""


