from __future__ import annotations

import logging
import sys
from types import ModuleType

import pytest

from autorole import pipeline as dryrun_pipeline
from autorole.integrations.discovery import build_discovery_providers
from autorole.integrations.discovery.smartextract import SmartExtractDiscoveryProvider
from autorole.config import SearchFilter
from autorole.integrations.discovery.workday import WorkdayDiscoveryProvider
from autorole.stages.exploring import ExploringStage
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