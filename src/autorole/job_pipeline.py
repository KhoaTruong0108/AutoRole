from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from autorole.config import AppConfig
from autorole.context import JobApplicationContext
from autorole.db.repository import JobRepository
from autorole.integrations.credentials import CredentialStore
from autorole.integrations.discovery import build_discovery_providers
from autorole.integrations.llm import AnthropicLLMClient, OllamaLLMClient, OpenAILLMClient
from autorole.integrations.renderer import PandocRenderer, WeasyPrintRenderer
from autorole.integrations.scrapers.indeed import IndeedScraper
from autorole.integrations.scrapers.linkedin import LinkedInScraper
from autorole.integrations.scrapers.url_posting import GenericJobPostingExtractor
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
    InMemoryQueueBackend,
    Message,
)
from autorole.stage_base import STAGE_ORDER
from autorole.stages.concluding import ConcludingStage
from autorole.stages.exploring import ExploringStage, ManualUrlExploringStage
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


@dataclass
class RunConfig:
    mode: str = "observe"
    platforms: list[str] = field(default_factory=lambda: ["linkedin", "indeed"])
    job_url: str = ""
    job_platform: str = ""
    keywords: list[str] = field(default_factory=list)
    location: str = ""
    max_listings: int = 1
    headless: bool = False
    resume_run_id: str = ""
    from_stage: str = ""


def _next_stage(stage_name: str) -> str | None:
    try:
        idx = STAGE_ORDER.index(stage_name)
    except ValueError:
        return None
    next_idx = idx + 1
    if next_idx >= len(STAGE_ORDER):
        return None
    return STAGE_ORDER[next_idx]


def _parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _configure_trace_logger(base_dir: Path) -> tuple[logging.Logger, Path]:
    logs_dir = base_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    log_path = logs_dir / f"real_pipeline_{stamp}.log"

    logger = logging.getLogger("autorole.real_runner")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(stderr_handler)
    return logger, log_path


async def init_db(db: aiosqlite.Connection) -> None:
    migration_001 = Path("src/autorole/db/migrations/001_domain.sql")
    migration_002 = Path("src/autorole/db/migrations/002_queue.sql")
    await db.executescript(migration_001.read_text(encoding="utf-8"))
    await db.executescript(migration_002.read_text(encoding="utf-8"))
    await db.commit()


def make_llm_client(config: AppConfig) -> OpenAILLMClient | AnthropicLLMClient | OllamaLLMClient:
    if config.llm.provider == "openai":
        return OpenAILLMClient(config.llm)
    if config.llm.provider == "ollama":
        return OllamaLLMClient(config.llm)
    return AnthropicLLMClient(config.llm)


def make_renderer(config: AppConfig) -> PandocRenderer | WeasyPrintRenderer:
    if config.renderer.engine == "weasyprint":
        return WeasyPrintRenderer()
    return PandocRenderer(config.renderer.pandoc_path, config.renderer.template)


def _stage_to_queue(stage_name: str) -> str:
    mapping = {
        "exploring": EXPLORING_Q,
        "scoring": SCORING_Q,
        "tailoring": SCORING_Q,
        "qualification": SCORING_Q,
        "packaging": PACKAGING_Q,
        "session": SESSION_Q,
        "form_intelligence": FORM_INTEL_Q,
        "llm_field_completer": LLM_FIELD_COMPLETER_Q,
        "form_submission": FORM_SUB_Q,
        "concluding": CONCLUDING_Q,
    }
    return mapping.get(stage_name, EXPLORING_Q)


def _next_reply_queue(input_queue: str) -> str:
    mapping = {
        EXPLORING_Q: SCORING_Q,
        SCORING_Q: PACKAGING_Q,
        PACKAGING_Q: SESSION_Q,
        SESSION_Q: FORM_INTEL_Q,
        FORM_INTEL_Q: LLM_FIELD_COMPLETER_Q,
        LLM_FIELD_COMPLETER_Q: FORM_SUB_Q,
        FORM_SUB_Q: CONCLUDING_Q,
        CONCLUDING_Q: CONCLUDING_Q,
    }
    return mapping.get(input_queue, CONCLUDING_Q)


def _queue_stage_name(queue_name: str) -> str:
    mapping = {
        EXPLORING_Q: "exploring",
        SCORING_Q: "qualification",
        PACKAGING_Q: "packaging",
        SESSION_Q: "session",
        FORM_INTEL_Q: "form_intelligence",
        LLM_FIELD_COMPLETER_Q: "llm_field_completer",
        FORM_SUB_Q: "form_submission",
        CONCLUDING_Q: "concluding",
    }
    return mapping.get(queue_name, "exploring")


def _make_seed_message(
    run_id: str,
    payload: dict[str, Any],
    target_queue: str,
    metadata: dict[str, Any] | None = None,
) -> Message:
    seed_metadata = {"source": "real_runner"}
    if metadata:
        seed_metadata.update(metadata)
    return Message(
        run_id=run_id,
        stage=_queue_stage_name(target_queue),
        payload=payload,
        reply_queue=_next_reply_queue(target_queue),
        dead_letter_queue=DEAD_LETTER_Q,
        attempt=1,
        metadata=seed_metadata,
    )


class _CompletionTracker:
    def __init__(self, expected: int) -> None:
        self.expected = max(1, expected)
        self.completed = 0
        self.exit_code = 0
        self.reason = ""
        self.event = asyncio.Event()

    def set_expected(self, expected: int) -> None:
        self.expected = max(1, expected)
        if self.completed >= self.expected:
            self.event.set()

    def on_success(self, run_id: str) -> None:
        _ = run_id
        self.completed += 1
        if self.completed >= self.expected:
            self.event.set()

    def on_failure(self, run_id: str, reason: str) -> None:
        self.exit_code = 1
        self.reason = f"run_id={run_id} reason={reason}"
        self.event.set()


class JobApplicationPipeline:
    STAGE_ORDER = STAGE_ORDER

    def __init__(self, config: AppConfig, run_config: RunConfig) -> None:
        self._config = config
        self._rc = run_config

    async def run(self) -> int:
        try:
            from playwright.async_api import async_playwright
        except Exception as exc:
            print("Playwright is required for real runs.")
            print("Install with: python -m pip install playwright")
            print("Then install browser with: python -m playwright install chromium")
            print(f"Import error: {exc}")
            return 2

        config = self._config
        rc = self._rc
        base_dir = Path(config.base_dir).expanduser()
        resume_dir = Path(config.resume_dir).expanduser()
        db_path = Path(config.db_path).expanduser()
        stage_outputs_root = base_dir / "logs" / "runs"

        base_dir.mkdir(parents=True, exist_ok=True)
        trace_logger, trace_log_path = _configure_trace_logger(base_dir)
        trace_logger.info("Starting event-driven pipeline run mode=%s", rc.mode)
        stage_outputs_root.mkdir(parents=True, exist_ok=True)
        print(f"Trace log: {trace_log_path}")
        resume_dir.mkdir(parents=True, exist_ok=True)

        if not Path(config.master_resume).expanduser().exists():
            print(f"Missing master resume: {Path(config.master_resume).expanduser()}")
            return 2

        is_manual_url_mode = bool(rc.job_url.strip())
        is_resume_mode = bool(rc.resume_run_id.strip())
        platforms = [p.strip() for p in rc.platforms if p.strip()]

        search_config = config.search.model_dump()
        if platforms:
            search_config["platforms"] = platforms
        if rc.keywords:
            search_config["keywords"] = [k.strip() for k in rc.keywords if k.strip()]
        if rc.location:
            search_config["location"] = rc.location

        if not is_resume_mode and not is_manual_url_mode and not platforms:
            print("No platforms selected")
            return 2

        try:
            async with aiosqlite.connect(db_path) as db:
                await init_db(db)
                repo = JobRepository(db)

                llm_client = make_llm_client(config)
                renderer = make_renderer(config)
                queue = InMemoryQueueBackend()
                tracker = _CompletionTracker(expected=rc.max_listings)

                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(headless=rc.headless)
                    browser_context = await browser.new_context()

                    scrape_page = await browser_context.new_page()
                    score_page = await browser_context.new_page()
                    form_page = await browser_context.new_page()

                    async def render_html(url: str) -> str:
                        await scrape_page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                        if "remoteok.com" in url:
                            with contextlib.suppress(Exception):
                                await scrape_page.wait_for_selector(
                                    "tr.job[data-id], tr.job[id^='job-']",
                                    timeout=5_000,
                                )
                            await scrape_page.wait_for_timeout(1_500)
                        return await scrape_page.content()

                    scrapers: dict[str, Any] = {}
                    if "linkedin" in platforms:
                        scrapers["linkedin"] = LinkedInScraper(scrape_page)
                    if "indeed" in platforms:
                        scrapers["indeed"] = IndeedScraper(scrape_page)

                    if is_manual_url_mode:
                        extractor = GenericJobPostingExtractor(scrape_page)
                        platform_hint = rc.job_platform.strip() or None
                        exploring_stage = ManualUrlExploringStage(config, extractor=extractor, platform_hint=platform_hint)
                        seed_payload: dict[str, Any] = {"job_url": rc.job_url.strip(), "max_listings": rc.max_listings}
                    else:
                        discovery_providers = build_discovery_providers(
                            platforms,
                            llm_client=llm_client,
                            render_html=render_html,
                        )
                        exploring_stage = ExploringStage(
                            config,
                            scrapers=scrapers,
                            discovery_providers=discovery_providers,
                        )
                        seed_payload = {"search_config": search_config, "max_listings": rc.max_listings}

                    workers = self._build_workers(
                        repo=repo,
                        logger=trace_logger,
                        stage_outputs_root=stage_outputs_root,
                        llm_client=llm_client,
                        renderer=renderer,
                        score_page=score_page,
                        form_page=form_page,
                        exploring_stage=exploring_stage,
                        tracker=tracker,
                    )

                    if is_resume_mode:
                        checkpoint = await repo.get_checkpoint(rc.resume_run_id.strip())
                        if checkpoint is None:
                            print(f"No checkpoint found for run_id={rc.resume_run_id.strip()}")
                            return 1

                        last_stage, checkpoint_ctx = checkpoint
                        resume_ctx = JobApplicationContext.model_validate(checkpoint_ctx)
                        start_stage = rc.from_stage or _next_stage(last_stage)
                        if start_stage is None:
                            print(
                                f"Checkpoint for run_id={resume_ctx.run_id} already completed at stage={last_stage}"
                            )
                            return 0

                        start_queue = _stage_to_queue(start_stage)
                        if rc.mode == "observe" and start_queue not in {EXPLORING_Q, SCORING_Q}:
                            print("[stop] observe mode; skipping packaging, session, and submission stages")
                            return 0
                        if rc.mode == "apply-dryrun" and start_queue == CONCLUDING_Q:
                            print("[stop] apply-dryrun mode enabled; completed flow with submit click skipped")
                            return 0
                        seed_metadata = {
                            "dryrun_stop_after_submit": rc.mode == "apply-dryrun",
                            "run_mode": rc.mode,
                        }
                        seed_msg = _make_seed_message(
                            resume_ctx.run_id,
                            resume_ctx.model_dump(mode="json"),
                            start_queue,
                            metadata=seed_metadata,
                        )
                        await queue.enqueue(start_queue, seed_msg)
                        print(
                            f"Resuming run_id={resume_ctx.run_id} from stage={start_stage} "
                            f"(last successful stage: {last_stage})"
                        )
                    else:
                        seed_metadata = {
                            "dryrun_stop_after_submit": rc.mode == "apply-dryrun",
                            "run_mode": rc.mode,
                        }
                        seed_msg = _make_seed_message(
                            "seed",
                            seed_payload,
                            EXPLORING_Q,
                            metadata=seed_metadata,
                        )
                        await queue.enqueue(EXPLORING_Q, seed_msg)

                    tasks = [asyncio.create_task(worker.run_forever(queue)) for worker in workers.values()]
                    try:
                        await asyncio.wait_for(tracker.event.wait(), timeout=3600.0)
                    except asyncio.TimeoutError:
                        trace_logger.error("Pipeline timed out")
                        return 1
                    finally:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        with contextlib.suppress(Exception):
                            await browser_context.close()
                        with contextlib.suppress(Exception):
                            await browser.close()
                if tracker.exit_code != 0:
                    if tracker.reason:
                        trace_logger.error("Pipeline failed terminally: %s", tracker.reason)
                    return tracker.exit_code
        except Exception:
            trace_logger.exception("Unhandled exception in event-driven pipeline runner")
            print("[fail] runner: unhandled exception (see trace log)")
            print(f"Trace log: {trace_log_path}")
            return 1

        print("\nRun completed.")
        print(f"DB: {db_path}")
        print("Use CLI to verify:")
        print("  PYTHONPATH=src python -m autorole.cli.main status")
        print("  PYTHONPATH=src python -m autorole.cli.main score <run_id>")
        print("  PYTHONPATH=src python -m autorole.cli.main diff <run_id>")
        print(f"Trace log: {trace_log_path}")
        return 0

    def _build_workers(
        self,
        repo: JobRepository,
        logger: logging.Logger,
        stage_outputs_root: Path,
        llm_client: Any,
        renderer: Any,
        score_page: Any,
        form_page: Any,
        exploring_stage: Any,
        tracker: _CompletionTracker,
    ) -> dict[str, Any]:
        cfg = self._config
        shared = {
            "repo": repo,
            "logger": logger,
            "artifacts_root": stage_outputs_root,
        }

        def wc(input_queue: str, reply_queue: str) -> WorkerConfig:
            return WorkerConfig(
                input_queue=input_queue,
                reply_queue=reply_queue,
                dead_letter_queue=DEAD_LETTER_Q,
                poll_interval_seconds=0.0,
                visibility_timeout_seconds=300,
                max_attempts=cfg.tailoring.max_attempts,
            )

        form_submission_use_gate = self._rc.mode != "apply-dryrun"

        workers: dict[str, Any] = {
            "exploring": ExploringWorker(
                stage=exploring_stage,
                config=wc(EXPLORING_Q, SCORING_Q),
                on_fanout=tracker.set_expected,
                on_block=tracker.on_failure,
                **shared,
            ),
            "qualification": QualificationWorker(
                scoring_stage=ScoringStage(cfg, llm_client, score_page),
                tailoring_stage=TailoringStage(cfg, llm_client),
                config=wc(SCORING_Q, PACKAGING_Q),
                max_attempts=cfg.tailoring.max_attempts,
                on_pass=tracker.on_success if self._rc.mode == "observe" else None,
                on_block=tracker.on_failure,
                **shared,
            ),
        }

        if self._rc.mode in {"apply", "apply-dryrun"}:
            workers["packaging"] = PackagingWorker(
                stage=PackagingStage(cfg, renderer),
                config=wc(PACKAGING_Q, SESSION_Q),
                on_block=tracker.on_failure,
                **shared,
            )
            workers["session"] = SessionWorker(
                stage=SessionStage(cfg, CredentialStore()),
                config=wc(SESSION_Q, FORM_INTEL_Q),
                on_block=tracker.on_failure,
                **shared,
            )
            workers["form_intelligence"] = FormIntelligenceWorker(
                stage=FormIntelligenceStage(
                    cfg,
                    llm_client,
                    form_page,
                ),
                config=wc(FORM_INTEL_Q, LLM_FIELD_COMPLETER_Q),
                on_block=tracker.on_failure,
                **shared,
            )
            workers["llm_field_completer"] = LLMFieldCompleterWorker(
                stage=LLMFieldCompleterStage(
                    cfg,
                    llm_client,
                    use_random_questionnaire_answers=self._rc.mode in {"observe"},
                ),
                config=wc(LLM_FIELD_COMPLETER_Q, FORM_SUB_Q),
                on_block=tracker.on_failure,
                **shared,
            )
            workers["form_submission"] = FormSubmissionWorker(
                stage=FormSubmissionStage(cfg, form_page),
                config=wc(FORM_SUB_Q, CONCLUDING_Q),
                use_form_gate=form_submission_use_gate,
                on_pass=tracker.on_success if self._rc.mode == "apply-dryrun" else None,
                on_block=tracker.on_failure,
                **shared,
            )

        if self._rc.mode == "apply":
            workers["concluding"] = ConcludingWorker(
                stage=ConcludingStage(cfg, repo),
                config=wc(CONCLUDING_Q, CONCLUDING_Q),
                done_callback=tracker.on_success,
                on_block=tracker.on_failure,
                **shared,
            )
        return workers


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="AutoRole pipeline runner")
    parser.add_argument("--dry-run", action="store_true", help="Build and run stub pipeline only")
    return parser.parse_args()


__all__ = ["JobApplicationPipeline", "RunConfig"]
