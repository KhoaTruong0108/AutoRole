from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from autorole.config import AppConfig
from autorole.context import JobListing
from autorole.integrations.scrapers.base import JobBoardScraper


@pytest.fixture
def test_config(tmp_path: Path) -> AppConfig:
	base_dir = tmp_path / "autorole"
	resume_dir = base_dir / "resumes"
	resume_dir.mkdir(parents=True, exist_ok=True)

	master_resume = resume_dir / "master.md"
	master_resume.write_text("# Test Resume\n\nExperience placeholder\n")

	return AppConfig(
		base_dir=str(base_dir),
		resume_dir=str(resume_dir),
		db_path=str(base_dir / "pipeline.db"),
		master_resume=str(master_resume),
	)


SAMPLE_LISTING = JobListing(
	job_url="https://example.com/jobs/3847291",
	company_name="Acme Cloud",
	job_id="3847291",
	job_title="Senior Backend Engineer",
	platform="linkedin",
	crawled_at=datetime.now(timezone.utc),
)

SAMPLE_JD_HTML = (
	"<html><body><h1>Senior Backend Engineer</h1>"
	"<ul><li>Python</li><li>Kubernetes</li></ul></body></html>"
)


class MockLLMClient:
	def __init__(self, response: Any = None) -> None:
		self.response = response

	async def call(self, *args: Any, **kwargs: Any) -> Any:
		_ = (args, kwargs)
		return self.response


class MockPage:
	def __init__(self, html: str = "") -> None:
		self._html = html
		self.navigation_count = 0

	async def goto(self, url: str, **kwargs: Any) -> None:
		_ = (url, kwargs)
		self.navigation_count += 1

	async def content(self) -> str:
		return self._html


class MockScraper(JobBoardScraper):
	def __init__(self, listings: list[JobListing]) -> None:
		self._listings = listings

	async def search(self, filters: Any) -> list[JobListing]:
		_ = filters
		return self._listings
