from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
import pytest_asyncio

from autorole.config import AppConfig
from autorole.context import JobListing
from autorole.db.repository import JobRepository
from autorole.integrations.scrapers.base import JobBoardScraper
from autorole.job_pipeline import init_db
from autorole.queue import DEAD_LETTER_Q, Message


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

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
	"""Load a JSON fixture by filename from tests/fixtures/."""
	return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def make_worker_message(ctx_dict: dict[str, Any], input_queue: str, reply_queue: str) -> Message:
	return Message(
		run_id=str(ctx_dict.get("run_id", "test-run-001")),
		stage=input_queue.removesuffix("_q"),
		payload=ctx_dict,
		reply_queue=reply_queue,
		dead_letter_queue=DEAD_LETTER_Q,
	)


class MockLLMClient:
	def __init__(self, response: Any = None) -> None:
		self.response = response

	async def call(self, *args: Any, **kwargs: Any) -> Any:
		_ = (args, kwargs)
		return self.response


class MockStage:
	"""Stub for any stage. Returns a pre-configured result on execute()."""

	def __init__(self, result: Any) -> None:
		self._result = result

	async def execute(self, message: Any) -> Any:
		_ = message
		return self._result


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


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> Any:
	db_path = tmp_path / "test.db"
	async with aiosqlite.connect(db_path) as conn:
		await init_db(conn)
		yield conn


@pytest_asyncio.fixture
async def repo(db: Any) -> JobRepository:
	return JobRepository(db)
