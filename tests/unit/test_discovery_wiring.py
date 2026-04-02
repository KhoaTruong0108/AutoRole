from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from types import ModuleType

import pytest

from autorole import pipeline as dryrun_pipeline
from autorole.integrations.discovery import build_discovery_providers
from autorole.integrations.discovery.smartextract import SmartExtractDiscoveryProvider
from autorole.config import SearchFilter
from autorole.integrations.discovery.workday import WorkdayDiscoveryProvider
from autorole.stages.exploring import ExploringStage, UrlListFileExploringStage
from autorole.queue import Message, SqliteQueueBackend
from autorole.workers import devrun as workers_devrun
from autorole.workers import run as workers_run
from autorole.workers.exploring import ExploringWorker


@pytest.mark.asyncio
async def test_build_pipeline_includes_workday_provider(test_config) -> None:
	config = test_config.model_copy(update={"search": SearchFilter(platforms=["workday"], keywords=["python"] )})

	_, exploring = await dryrun_pipeline.build_pipeline(config)

	assert isinstance(exploring, ExploringStage)
	assert "workday" in exploring._discovery_providers
	assert isinstance(exploring._discovery_providers["workday"], WorkdayDiscoveryProvider)


class _FakePage:
	pass


class _FakeBrowserContext:
	async def new_page(self) -> _FakePage:
		return _FakePage()

	async def close(self) -> None:
		return None


class _FakeBrowser:
	async def new_context(self) -> _FakeBrowserContext:
		return _FakeBrowserContext()

	async def close(self) -> None:
		return None


class _FakeChromium:
	async def launch(self, headless: bool = True) -> _FakeBrowser:
		_ = headless
		return _FakeBrowser()


class _FakePlaywright:
	def __init__(self) -> None:
		self.chromium = _FakeChromium()

	async def stop(self) -> None:
		return None


class _FakeAsyncPlaywrightManager:
	async def start(self) -> _FakePlaywright:
		return _FakePlaywright()


@pytest.mark.asyncio
async def test_build_worker_includes_workday_provider(monkeypatch, test_config, repo, tmp_path) -> None:
	config = test_config.model_copy(update={"search": SearchFilter(platforms=["workday"], keywords=["python"])})
	monkeypatch.setattr(workers_run, "AppConfig", lambda: config)
	monkeypatch.setattr(workers_run, "_make_llm_client", lambda _config: object())
	monkeypatch.setattr(workers_run, "_make_renderer", lambda _config: object())

	fake_async_api = ModuleType("playwright.async_api")
	fake_async_api.async_playwright = lambda: _FakeAsyncPlaywrightManager()
	fake_playwright = ModuleType("playwright")
	fake_playwright.async_api = fake_async_api
	monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
	monkeypatch.setitem(sys.modules, "playwright.async_api", fake_async_api)

	worker = await workers_run._build_worker(
		"exploring",
		repo,
		logging.getLogger("test.discovery.wiring"),
		tmp_path,
	)

	assert isinstance(worker, ExploringWorker)
	assert isinstance(worker._stage, ExploringStage)
	assert "workday" in worker._stage._discovery_providers
	assert isinstance(worker._stage._discovery_providers["workday"], WorkdayDiscoveryProvider)


@pytest.mark.asyncio
async def test_devrun_build_worker_uses_payload_platforms_for_exploring(monkeypatch, test_config, repo, tmp_path) -> None:
	config = test_config.model_copy(update={"search": SearchFilter(platforms=["linkedin", "indeed"], keywords=["python"])})
	monkeypatch.setattr(workers_devrun, "_make_llm_client", lambda _config: object())
	monkeypatch.setattr(workers_devrun, "_make_renderer", lambda _config: object())

	fake_async_api = ModuleType("playwright.async_api")
	fake_async_api.async_playwright = lambda: _FakeAsyncPlaywrightManager()
	fake_playwright = ModuleType("playwright")
	fake_playwright.async_api = fake_async_api
	monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
	monkeypatch.setitem(sys.modules, "playwright.async_api", fake_async_api)

	worker, _playwright, _closable, _page = await workers_devrun._build_worker(
		"exploring",
		config,
		repo,
		logging.getLogger("test.discovery.wiring"),
		tmp_path,
		headless=True,
		search_platforms=workers_devrun._effective_search_platforms(
			config,
			{"search_config": {"platforms": ["workday"]}},
			"exploring",
		),
	)

	assert isinstance(worker, ExploringWorker)
	assert isinstance(worker._stage, ExploringStage)
	assert "workday" in worker._stage._discovery_providers
	assert isinstance(worker._stage._discovery_providers["workday"], WorkdayDiscoveryProvider)


@pytest.mark.asyncio
async def test_devrun_build_worker_uses_url_list_stage_when_payload_present(monkeypatch, test_config, repo, tmp_path) -> None:
	monkeypatch.setattr(workers_devrun, "_make_llm_client", lambda _config: object())
	monkeypatch.setattr(workers_devrun, "_make_renderer", lambda _config: object())

	fake_async_api = ModuleType("playwright.async_api")
	fake_async_api.async_playwright = lambda: _FakeAsyncPlaywrightManager()
	fake_playwright = ModuleType("playwright")
	fake_playwright.async_api = fake_async_api
	monkeypatch.setitem(sys.modules, "playwright", fake_playwright)
	monkeypatch.setitem(sys.modules, "playwright.async_api", fake_async_api)

	worker, _playwright, _closable, _page = await workers_devrun._build_worker(
		"exploring",
		test_config,
		repo,
		logging.getLogger("test.discovery.wiring"),
		tmp_path,
		headless=True,
		payload={"job_urls_file": str(tmp_path / "job_urls.json")},
	)

	assert isinstance(worker, ExploringWorker)
	assert isinstance(worker._stage, UrlListFileExploringStage)


@pytest.mark.asyncio
async def test_devrun_load_payload_accepts_job_urls_file(repo, tmp_path) -> None:
	job_urls_file = tmp_path / "job_urls.json"
	args = argparse.Namespace(
		input_run_id="",
		input_file="",
		job_urls_file=str(job_urls_file),
		job_platform="greenhouse",
	)

	payload = await workers_devrun._load_payload(args, repo)

	assert payload == {
		"job_urls_file": str(job_urls_file),
		"job_platform": "greenhouse",
	}


@pytest.mark.asyncio
async def test_devrun_load_payload_requires_one_input_source(repo) -> None:
	args = argparse.Namespace(
		input_run_id="",
		input_file="",
		job_urls_file="",
		job_platform="",
	)

	with pytest.raises(ValueError, match="One of --input-run-id, --input-file, or --job-urls-file is required"):
		await workers_devrun._load_payload(args, repo)


def test_build_discovery_providers_passes_llm_to_smartextract() -> None:
	llm_client = object()
	providers = build_discovery_providers(["smartextract"], llm_client=llm_client)

	assert isinstance(providers["smartextract"], SmartExtractDiscoveryProvider)
	assert providers["smartextract"]._llm is llm_client


def test_devrun_listing_summary_extracts_core_listing_fields() -> None:
	payload = {
		"listing": {
			"company_name": "Acme",
			"job_title": "Backend Engineer",
			"platform": "workday",
			"job_id": "JR-42",
			"job_url": "https://example.com/jobs/42",
			"apply_url": "https://example.com/jobs/42/apply",
		}
	}

	assert workers_devrun._listing_summary(payload) == {
		"company_name": "Acme",
		"job_title": "Backend Engineer",
		"platform": "workday",
		"job_id": "JR-42",
		"job_url": "https://example.com/jobs/42",
	}


@pytest.mark.asyncio
async def test_devrun_peek_queue_message_returns_oldest_queued(db) -> None:
	queue = SqliteQueueBackend(db)
	first = Message(
		run_id="run-1",
		stage="scoring",
		payload={"value": 1},
		reply_queue="packaging_q",
		dead_letter_queue="dead_letter_q",
	)
	second = Message(
		run_id="run-2",
		stage="scoring",
		payload={"value": 2},
		reply_queue="packaging_q",
		dead_letter_queue="dead_letter_q",
	)
	await queue.enqueue("scoring_q", first)
	await queue.enqueue("scoring_q", second)

	peeked = await workers_devrun._peek_queue_message(db, "scoring_q")

	assert peeked is not None
	assert peeked.message_id == first.message_id
	assert peeked.payload == {"value": 1}


@pytest.mark.asyncio
async def test_devrun_peek_queue_message_can_select_message_id(db) -> None:
	queue = SqliteQueueBackend(db)
	first = Message(
		run_id="run-1",
		stage="scoring",
		payload={"value": 1},
		reply_queue="packaging_q",
		dead_letter_queue="dead_letter_q",
	)
	second = Message(
		run_id="run-2",
		stage="scoring",
		payload={"value": 2},
		reply_queue="packaging_q",
		dead_letter_queue="dead_letter_q",
	)
	await queue.enqueue("scoring_q", first)
	await queue.enqueue("scoring_q", second)

	peeked = await workers_devrun._peek_queue_message(db, "scoring_q", message_id=second.message_id)

	assert peeked is not None
	assert peeked.message_id == second.message_id
	assert peeked.payload == {"value": 2}


@pytest.mark.asyncio
async def test_devrun_peek_queue_message_ignores_invisible_messages(db) -> None:
	message = Message(
		run_id="run-1",
		stage="scoring",
		payload={"value": 1},
		reply_queue="packaging_q",
		dead_letter_queue="dead_letter_q",
	)
	now = datetime.now(timezone.utc)
	visible_after = (now + timedelta(minutes=5)).isoformat()
	await db.execute(
		"""
		INSERT INTO queue_messages (
			message_id, queue_name, run_id, stage, payload, attempt,
			reply_queue, dead_letter_queue, metadata, status, enqueued_at, visible_after
		) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
		""",
		(
			message.message_id,
			"scoring_q",
			message.run_id,
			message.stage,
			'{"value": 1}',
			1,
			message.reply_queue,
			message.dead_letter_queue,
			'{}',
			"queued",
			now.isoformat(),
			visible_after,
		),
	)
	await db.commit()

	peeked = await workers_devrun._peek_queue_message(db, "scoring_q")

	assert peeked is None


def test_devrun_message_mode_prefers_message_metadata() -> None:
	message = Message(
		run_id="run-1",
		stage="scoring",
		payload={},
		reply_queue="packaging_q",
		dead_letter_queue="dead_letter_q",
		metadata={"run_mode": "apply-dryrun"},
	)

	assert workers_devrun._message_mode(message, "observe") == "apply-dryrun"
	assert workers_devrun._message_mode(message, "apply-dryrun") == "apply-dryrun"
	assert workers_devrun._message_mode(None, "observe") == "observe"