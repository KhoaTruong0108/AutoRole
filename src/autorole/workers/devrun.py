from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from autorole.config import AppConfig
from autorole.context import JobApplicationContext
from autorole.db.repository import JobRepository
from autorole.integrations.credentials import CredentialStore
from autorole.integrations.llm import AnthropicLLMClient, OllamaLLMClient, OpenAILLMClient
from autorole.integrations.renderer import PandocRenderer, WeasyPrintRenderer
from autorole.integrations.scrapers.indeed import IndeedScraper
from autorole.integrations.scrapers.linkedin import LinkedInScraper
from autorole.integrations.scrapers.url_posting import GenericJobPostingExtractor
from autorole.job_pipeline import init_db
from autorole.queue import (
    CONCLUDING_Q,
    DEAD_LETTER_Q,
    EXPLORING_Q,
    FORM_INTEL_Q,
    LLM_FIELD_COMPLETER_Q,
    FORM_SUB_Q,
    InMemoryQueueBackend,
    Message,
    PACKAGING_Q,
    SCORING_Q,
    SESSION_Q,
)
from autorole.stages.concluding import ConcludingStage
from autorole.stages.exploring import ExploringStage, ManualUrlExploringStage
from autorole.stages.form_intelligence import FormIntelligenceStage
from autorole.stages.llm_field_completer import LLMFieldCompleterStage
from autorole.stages.form_submission import FormSubmissionStage
from autorole.stages.packaging import PackagingStage
from autorole.stages.scoring import ScoringStage
from autorole.stages.session import SessionStage
from autorole.stages.tailoring import TailoringStage
from autorole.workers.base import StageWorker, WorkerConfig
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


def _stage_to_queues(stage: str) -> tuple[str, str]:
    mapping = {
        "exploring": (EXPLORING_Q, SCORING_Q),
        "qualification": (SCORING_Q, PACKAGING_Q),
        "packaging": (PACKAGING_Q, SESSION_Q),
        "session": (SESSION_Q, FORM_INTEL_Q),
        "form_intelligence": (FORM_INTEL_Q, LLM_FIELD_COMPLETER_Q),
        "llm_field_completer": (LLM_FIELD_COMPLETER_Q, FORM_SUB_Q),
        "form_submission": (FORM_SUB_Q, CONCLUDING_Q),
        "concluding": (CONCLUDING_Q, CONCLUDING_Q),
    }
    if stage not in mapping:
        raise ValueError(f"Unsupported stage: {stage}")
    return mapping[stage]


def _build_message(
    payload: dict[str, Any],
    input_queue: str,
    reply_queue: str,
    *,
    stage: str,
    mode: str,
) -> Message:
    metadata: dict[str, Any] = {"run_mode": mode}
    if stage == "form_submission" and mode == "apply-dryrun":
        metadata["dryrun_stop_after_submit"] = True
    return Message(
        run_id=str(payload.get("run_id", "devrun-seed")),
        stage=input_queue.removesuffix("_q"),
        payload=payload,
        reply_queue=reply_queue,
        dead_letter_queue=DEAD_LETTER_Q,
        metadata=metadata,
    )


async def _build_worker(
    stage: str,
    cfg: AppConfig,
    repo: JobRepository,
    logger: logging.Logger,
    artifacts_root: Path,
    *,
    headless: bool,
) -> tuple[StageWorker, Any, Any, Any]:
    llm_client = _make_llm_client(cfg)
    renderer = _make_renderer(cfg)
    input_q, reply_q = _stage_to_queues(stage)
    worker_cfg = WorkerConfig(
        input_queue=input_q,
        reply_queue=reply_q,
        dead_letter_queue=DEAD_LETTER_Q,
        poll_interval_seconds=0,
    )

    if stage in {"qualification", "session", "form_intelligence", "llm_field_completer", "form_submission", "exploring"}:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()
    else:
        playwright = browser = context = page = None

    shared = {
        "repo": repo,
        "logger": logger,
        "artifacts_root": artifacts_root,
        "config": worker_cfg,
    }

    if stage == "exploring":
        scrapers = {
            "linkedin": LinkedInScraper(page),
            "indeed": IndeedScraper(page),
        }
        worker = ExploringWorker(stage=ExploringStage(cfg, scrapers=scrapers), **shared)
    elif stage == "qualification":
        worker = QualificationWorker(
            scoring_stage=ScoringStage(cfg, llm_client, page),
            tailoring_stage=TailoringStage(cfg, llm_client),
            max_attempts=cfg.tailoring.max_attempts,
            **shared,
        )
    elif stage == "packaging":
        worker = PackagingWorker(stage=PackagingStage(cfg, renderer), **shared)
    elif stage == "session":
        worker = SessionWorker(stage=SessionStage(cfg, CredentialStore()), **shared)
    elif stage == "form_intelligence":
        worker = FormIntelligenceWorker(stage=FormIntelligenceStage(cfg, llm_client, page), **shared)
    elif stage == "llm_field_completer":
        worker = LLMFieldCompleterWorker(stage=LLMFieldCompleterStage(cfg, llm_client), **shared)
    elif stage == "form_submission":
        worker = FormSubmissionWorker(stage=FormSubmissionStage(cfg, page), **shared)
    elif stage == "concluding":
        worker = ConcludingWorker(stage=ConcludingStage(cfg, repo), **shared)
    else:
        raise ValueError(f"Unsupported stage: {stage}")

    return worker, playwright, browser or context, page


def _resolve_apply_url(payload: dict[str, Any]) -> str:
    form_session = payload.get("form_session")
    if isinstance(form_session, dict):
        detection = form_session.get("detection")
        if isinstance(detection, dict):
            apply_url = detection.get("apply_url")
            if isinstance(apply_url, str) and apply_url.strip():
                return apply_url.strip()

    listing = payload.get("listing")
    if isinstance(listing, dict):
        apply_url = listing.get("apply_url")
        if isinstance(apply_url, str) and apply_url.strip():
            return apply_url.strip()
        job_url = listing.get("job_url")
        if isinstance(job_url, str) and job_url.strip():
            return job_url.strip()

    return ""


async def _prepare_stage_page(stage: str, payload: dict[str, Any], page: Any) -> None:
    if page is None:
        return
    if stage not in {"session", "form_intelligence", "form_submission"}:
        return

    url = _resolve_apply_url(payload)
    if not url:
        return

    try:
        await page.goto(url, wait_until="domcontentloaded")
    except Exception:
        # Keep devrun resilient; stage execution will surface the concrete error path.
        pass


async def _load_payload(args: argparse.Namespace, repo: JobRepository) -> dict[str, Any]:
    if args.input_run_id:
        checkpoint = await repo.get_checkpoint(args.input_run_id)
        if checkpoint is None:
            raise ValueError(f"No checkpoint found for run_id={args.input_run_id}")
        return checkpoint[1]

    if not args.input_file:
        raise ValueError("One of --input-run-id or --input-file is required")

    return json.loads(Path(args.input_file).read_text(encoding="utf-8"))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one worker.process() for stage development")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--input-run-id", default="")
    parser.add_argument("--input-file", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--mode", choices=["observe", "apply", "apply-dryrun"], default="observe")
    return parser.parse_args()


async def amain() -> int:
    args = _parse_args()
    if args.mode == "observe" and args.stage in {"session", "form_intelligence", "llm_field_completer", "form_submission"}:
        print(f"[warn] observe mode skips stage '{args.stage}'")
        return 0

    cfg = AppConfig()
    logger = logging.getLogger("autorole.workers.devrun")
    logger.setLevel(logging.INFO)
    artifacts_root = Path(cfg.base_dir).expanduser() / "logs" / "runs"
    artifacts_root.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(Path(cfg.db_path).expanduser()) as db:
        await init_db(db)
        repo = JobRepository(db)
        payload = await _load_payload(args, repo)
        input_q, reply_q = _stage_to_queues(args.stage)

        if args.dry_run:
            print(f"=== devrun: {args.stage} ===")
            print(f"run_id:   {payload.get('run_id', 'devrun-seed')}")
            print(f"input_q:  {input_q}")
            print(f"reply_q:  {reply_q}")
            print("mode:     dry-run")
            return 0

        queue = InMemoryQueueBackend()
        worker, playwright, closable, page = await _build_worker(
            args.stage,
            cfg,
            repo,
            logger,
            artifacts_root,
            headless=args.headless,
        )

        await _prepare_stage_page(args.stage, payload, page)

        msg = _build_message(
            payload,
            input_q,
            reply_q,
            stage=args.stage,
            mode=args.mode,
        )
        await queue.enqueue(input_q, msg)
        pulled = await queue.pull(input_q)
        assert pulled is not None

        try:
            await worker.process(queue, pulled)
            await asyncio.sleep(10)  # Wait for any async finalization in worker
        finally:
            try:
                if closable is not None:
                    await closable.close()
                if playwright is not None:
                    await playwright.stop()
            except Exception:
                pass

        out = await queue.pull(reply_q)
        dlq = await queue.pull(DEAD_LETTER_Q)
        loop_q = FORM_INTEL_Q if args.stage == "form_submission" else input_q
        loop_msg = await queue.pull(loop_q)

        decision = "pass"
        if dlq is not None:
            decision = "block"
        elif loop_msg is not None and (args.stage == "form_submission" or loop_msg.attempt > 1):
            decision = "loop"

        print(f"=== devrun: {args.stage} ===")
        print(f"run_id:   {msg.run_id}")
        print(f"decision: {decision}")
        print("")
        print(f"[output queue: {reply_q}]")
        if out is None:
            print("  empty")
        else:
            keys = ", ".join(sorted(out.payload.keys())) if isinstance(out.payload, dict) else type(out.payload).__name__
            print(f"  message_id: {out.message_id}")
            print(f"  payload keys: {keys}")

        print("")
        print("[dead_letter_q]")
        print("  empty" if dlq is None else f"  message_id: {dlq.message_id}")

        print("")
        print("[artifacts]")
        print(f"  {artifacts_root / msg.run_id / args.stage}")

        print("")
        print("[db checkpoint]")
        checkpoint = await repo.get_checkpoint(msg.run_id)
        if checkpoint is None:
            print("  none")
        else:
            print(f"  last_success_stage: {checkpoint[0]}")

    return 0


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
