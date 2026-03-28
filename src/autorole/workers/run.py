from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from autorole.config import AppConfig
from autorole.db.repository import JobRepository
from autorole.integrations.credentials import CredentialStore
from autorole.integrations.llm import AnthropicLLMClient, OllamaLLMClient, OpenAILLMClient
from autorole.integrations.renderer import PandocRenderer, WeasyPrintRenderer
from autorole.queue import (
    CONCLUDING_Q,
    DEAD_LETTER_Q,
    EXPLORING_Q,
    FORM_INTEL_Q,
    LLM_FIELD_COMPLETER_Q,
    FORM_SUB_Q,
    PACKAGING_Q,
    SCORING_Q,
    SESSION_Q,
    SqliteQueueBackend,
)
from autorole.stages.concluding import ConcludingStage
from autorole.stages.exploring import ExploringStage
from autorole.stages.form_intelligence import FormIntelligenceStage
from autorole.stages.llm_field_completer import LLMFieldCompleterStage
from autorole.stages.form_submission import FormSubmissionStage
from autorole.stages.packaging import PackagingStage
from autorole.stages.scoring import ScoringStage
from autorole.stages.session import SessionStage
from autorole.stages.tailoring import TailoringStage
from autorole.workers import WorkerConfig
from autorole.workers.concluding import ConcludingWorker
from autorole.workers.exploring import ExploringWorker
from autorole.workers.form_intelligence import FormIntelligenceWorker
from autorole.workers.llm_field_completer import LLMFieldCompleterWorker
from autorole.workers.form_submission import FormSubmissionWorker
from autorole.workers.packaging import PackagingWorker
from autorole.workers.qualification import QualificationWorker
from autorole.workers.session import SessionWorker


def _make_llm_client(config: AppConfig) -> OpenAILLMClient | AnthropicLLMClient | OllamaLLMClient:
    if config.llm.provider == "openai":
        return OpenAILLMClient(config.llm)
    if config.llm.provider == "ollama":
        return OllamaLLMClient(config.llm)
    return AnthropicLLMClient(config.llm)


def _make_renderer(config: AppConfig) -> PandocRenderer | WeasyPrintRenderer:
    if config.renderer.engine == "weasyprint":
        return WeasyPrintRenderer()
    return PandocRenderer(config.renderer.pandoc_path, config.renderer.template)


def _worker_config(input_queue: str, reply_queue: str) -> WorkerConfig:
    return WorkerConfig(
        input_queue=input_queue,
        reply_queue=reply_queue,
        dead_letter_queue=DEAD_LETTER_Q,
        poll_interval_seconds=2.0,
        visibility_timeout_seconds=300,
    )


async def _build_worker(stage_name: str, repo: JobRepository, logger: logging.Logger, artifacts_root: Path) -> Any:
    cfg = AppConfig()
    llm_client = _make_llm_client(cfg)
    renderer = _make_renderer(cfg)

    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        raise RuntimeError(f"Playwright is required for worker execution: {exc}") from exc

    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context()
    scrape_page = await context.new_page()
    score_page = await context.new_page()
    form_page = await context.new_page()

    shared = {"repo": repo, "logger": logger, "artifacts_root": artifacts_root}
    if stage_name == "exploring":
        return ExploringWorker(
            stage=ExploringStage(cfg, scrapers={}),
            config=_worker_config(EXPLORING_Q, SCORING_Q),
            **shared,
        )
    if stage_name == "qualification":
        return QualificationWorker(
            scoring_stage=ScoringStage(cfg, llm_client, score_page),
            tailoring_stage=TailoringStage(cfg, llm_client),
            config=_worker_config(SCORING_Q, PACKAGING_Q),
            max_attempts=cfg.tailoring.max_attempts,
            **shared,
        )
    if stage_name == "packaging":
        return PackagingWorker(
            stage=PackagingStage(cfg, renderer),
            config=_worker_config(PACKAGING_Q, SESSION_Q),
            **shared,
        )
    if stage_name == "session":
        return SessionWorker(
            stage=SessionStage(cfg, CredentialStore()),
            config=_worker_config(SESSION_Q, FORM_INTEL_Q),
            **shared,
        )
    if stage_name == "form_intelligence":
        return FormIntelligenceWorker(
            stage=FormIntelligenceStage(cfg, llm_client, form_page),
            config=_worker_config(FORM_INTEL_Q, LLM_FIELD_COMPLETER_Q),
            **shared,
        )
    if stage_name == "llm_field_completer":
        return LLMFieldCompleterWorker(
            stage=LLMFieldCompleterStage(cfg, llm_client),
            config=_worker_config(LLM_FIELD_COMPLETER_Q, FORM_SUB_Q),
            **shared,
        )
    if stage_name == "form_submission":
        return FormSubmissionWorker(
            stage=FormSubmissionStage(cfg, form_page),
            config=_worker_config(FORM_SUB_Q, CONCLUDING_Q),
            **shared,
        )
    if stage_name == "concluding":
        return ConcludingWorker(
            stage=ConcludingStage(cfg, repo),
            config=_worker_config(CONCLUDING_Q, CONCLUDING_Q),
            **shared,
        )

    await context.close()
    await browser.close()
    await playwright.stop()
    raise ValueError(f"Unsupported stage: {stage_name}")


async def amain() -> int:
    parser = argparse.ArgumentParser(description="Run a single AutoRole worker with SQLite backend")
    parser.add_argument("--stage", required=True)
    args = parser.parse_args()

    config = AppConfig()
    logger = logging.getLogger("autorole.worker.run")
    logger.setLevel(logging.INFO)
    artifacts_root = Path(config.base_dir).expanduser() / "logs" / "runs"
    artifacts_root.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(Path(config.db_path).expanduser()) as db:
        from autorole.job_pipeline import init_db

        await init_db(db)
        backend = SqliteQueueBackend(db)
        repo = JobRepository(db)
        worker = await _build_worker(args.stage, repo, logger, artifacts_root)
        await worker.run_forever(backend)

    return 0


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
