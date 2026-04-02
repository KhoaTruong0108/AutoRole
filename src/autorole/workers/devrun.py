from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
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
from autorole.job_pipeline import init_db
from autorole.queue import (
    CONCLUDING_Q,
    DEAD_LETTER_Q,
    EXPLORING_Q,
    FORM_INTEL_Q,
    LLM_FIELD_COMPLETER_Q,
    FORM_SUB_Q,
    Message,
    PACKAGING_Q,
    SCORING_Q,
    TAILORING_Q,
    SESSION_Q,
    SqliteQueueBackend,
)
from autorole.stages.concluding import ConcludingStage
from autorole.stages.exploring import ExploringStage, ManualUrlExploringStage, UrlListFileExploringStage
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
from autorole.workers.scoring import ScoringWorker
from autorole.workers.session import SessionWorker
from autorole.workers.tailoring import TailoringWorker


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
        "scoring": (SCORING_Q, TAILORING_Q),
        "tailoring": (TAILORING_Q, PACKAGING_Q),
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


def _listing_summary(payload: dict[str, Any]) -> dict[str, Any] | None:
    listing = payload.get("listing")
    if not isinstance(listing, dict):
        return None
    return {
        "company_name": listing.get("company_name"),
        "job_title": listing.get("job_title"),
        "platform": listing.get("platform"),
        "job_id": listing.get("job_id"),
        "job_url": listing.get("job_url"),
    }


def _print_json_block(prefix: str, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    for line in rendered.splitlines():
        print(f"{prefix}{line}")


def _effective_search_platforms(cfg: AppConfig, payload: dict[str, Any], stage: str) -> list[str]:
    if stage != "exploring":
        return list(cfg.search.platforms)

    search_config = payload.get("search_config")
    if not isinstance(search_config, dict):
        return list(cfg.search.platforms)

    raw_platforms = search_config.get("platforms")
    if not isinstance(raw_platforms, list):
        return list(cfg.search.platforms)

    platforms = [str(platform).strip() for platform in raw_platforms if str(platform).strip()]
    return platforms or list(cfg.search.platforms)


async def _build_worker(
    stage: str,
    cfg: AppConfig,
    repo: JobRepository,
    logger: logging.Logger,
    artifacts_root: Path,
    *,
    headless: bool,
    search_platforms: list[str] | None = None,
    payload: dict[str, Any] | None = None,
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

    if stage in {"scoring", "session", "form_intelligence", "llm_field_completer", "form_submission", "exploring"}:
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()

        async def render_html(url: str) -> str:
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            if "remoteok.com" in url:
                with contextlib.suppress(Exception):
                    await page.wait_for_selector(
                        "tr.job[data-id], tr.job[id^='job-']",
                        timeout=5_000,
                    )
                await page.wait_for_timeout(1_500)
            return await page.content()
    else:
        playwright = browser = context = page = None
        render_html = None

    shared = {
        "repo": repo,
        "logger": logger,
        "artifacts_root": artifacts_root,
        "config": worker_cfg,
    }

    if stage == "exploring":
        exploring_payload = payload if isinstance(payload, dict) else {}
        platform_hint = str(exploring_payload.get("job_platform", "")).strip() or None
        if isinstance(exploring_payload.get("job_url"), str) and exploring_payload.get("job_url", "").strip():
            worker = ExploringWorker(
                stage=ManualUrlExploringStage(
                    cfg,
                    extractor=GenericJobPostingExtractor(page),
                    platform_hint=platform_hint,
                ),
                **shared,
            )
        elif isinstance(exploring_payload.get("job_urls_file"), str) and exploring_payload.get("job_urls_file", "").strip():
            worker = ExploringWorker(
                stage=UrlListFileExploringStage(
                    cfg,
                    extractor=GenericJobPostingExtractor(page),
                    platform_hint=platform_hint,
                ),
                **shared,
            )
        else:
            active_platforms = search_platforms or list(cfg.search.platforms)
            scrapers = {
                "linkedin": LinkedInScraper(page),
                "indeed": IndeedScraper(page),
            }
            discovery_providers = build_discovery_providers(
                active_platforms,
                llm_client=llm_client,
                render_html=render_html,
            )
            worker = ExploringWorker(
                stage=ExploringStage(cfg, scrapers=scrapers, discovery_providers=discovery_providers),
                **shared,
            )
    elif stage == "scoring":
        worker = ScoringWorker(
            scoring_stage=ScoringStage(cfg, llm_client, page),
            **shared,
        )
    elif stage == "tailoring":
        worker = TailoringWorker(
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

    if args.job_urls_file:
        payload: dict[str, Any] = {"job_urls_file": args.job_urls_file}
        if args.job_platform.strip():
            payload["job_platform"] = args.job_platform.strip()
        return payload

    if not args.input_file:
        raise ValueError("One of --input-run-id, --input-file, or --job-urls-file is required")

    return json.loads(Path(args.input_file).read_text(encoding="utf-8"))


async def _peek_queue_message(
    db: aiosqlite.Connection,
    queue_name: str,
    *,
    message_id: str = "",
) -> Message | None:
    now_iso = datetime.now(timezone.utc).isoformat()
    params: list[object] = [queue_name, now_iso]
    query = """
        SELECT
            message_id,
            run_id,
            stage,
            payload,
            reply_queue,
            dead_letter_queue,
            attempt,
            metadata
        FROM queue_messages
        WHERE queue_name = ?
                    AND status IN ('queued', 'pending')
          AND visible_after <= ?
    """
    if message_id.strip():
        query += " AND message_id = ?"
        params.append(message_id.strip())
    query += " ORDER BY enqueued_at ASC LIMIT 1"

    async with db.execute(query, tuple(params)) as cur:
        row = await cur.fetchone()
    if row is None:
        return None

    return Message(
        message_id=str(row[0]),
        run_id=str(row[1]),
        stage=str(row[2]),
        payload=json.loads(row[3]),
        reply_queue=str(row[4]),
        dead_letter_queue=str(row[5]),
        attempt=int(row[6]),
        metadata=json.loads(row[7] or "{}"),
    )


async def _claim_queue_message(
    db: aiosqlite.Connection,
    queue_name: str,
    *,
    message_id: str = "",
    visibility_timeout_seconds: int = 300,
) -> Message | None:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    visible_after = (now.timestamp() + visibility_timeout_seconds)
    next_visible_after = datetime.fromtimestamp(visible_after, timezone.utc).isoformat()

    params: list[object] = [queue_name, now_iso]
    query = """
        SELECT
            message_id,
            run_id,
            stage,
            payload,
            reply_queue,
            dead_letter_queue,
            attempt,
            metadata
        FROM queue_messages
        WHERE queue_name = ?
                    AND status IN ('queued', 'pending')
          AND visible_after <= ?
    """
    if message_id.strip():
        query += " AND message_id = ?"
        params.append(message_id.strip())
    query += " ORDER BY enqueued_at ASC LIMIT 1"

    await db.execute("BEGIN IMMEDIATE")
    try:
        # Reclaim expired in-flight rows so local dev runs can resume processing without
        # requiring the long-running reaper task.
        await db.execute(
            """
            UPDATE queue_messages
            SET status = 'queued', visible_after = ?
            WHERE queue_name = ?
              AND status = 'processing'
              AND visible_after <= ?
            """,
            (now_iso, queue_name, now_iso),
        )

        async with db.execute(query, tuple(params)) as cur:
            row = await cur.fetchone()

        if row is None:
            await db.rollback()
            return None

        claimed_message_id = str(row[0])
        await db.execute(
            """
            UPDATE queue_messages
            SET status = 'processing', visible_after = ?
            WHERE message_id = ?
            """,
            (next_visible_after, claimed_message_id),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise

    return Message(
        message_id=str(row[0]),
        run_id=str(row[1]),
        stage=str(row[2]),
        payload=json.loads(row[3]),
        reply_queue=str(row[4]),
        dead_letter_queue=str(row[5]),
        attempt=int(row[6]),
        metadata=json.loads(row[7] or "{}"),
    )


async def _list_queue_messages(
    db: aiosqlite.Connection,
    queue_name: str,
    *,
    run_id: str = "",
) -> list[Message]:
    params: list[object] = [queue_name]
    query = """
        SELECT
            message_id,
            run_id,
            stage,
            payload,
            reply_queue,
            dead_letter_queue,
            attempt,
            metadata
        FROM queue_messages
        WHERE queue_name = ?
    """
    if run_id.strip():
        query += " AND run_id = ?"
        params.append(run_id.strip())
    query += " ORDER BY enqueued_at ASC"

    messages: list[Message] = []
    async with db.execute(query, tuple(params)) as cur:
        async for row in cur:
            messages.append(
                Message(
                    message_id=str(row[0]),
                    run_id=str(row[1]),
                    stage=str(row[2]),
                    payload=json.loads(row[3]),
                    reply_queue=str(row[4]),
                    dead_letter_queue=str(row[5]),
                    attempt=int(row[6]),
                    metadata=json.loads(row[7] or "{}"),
                )
            )
    return messages


def _message_mode(message: Message | None, fallback_mode: str) -> str:
    # Preserve queue message mode by default, but allow explicit CLI overrides.
    if fallback_mode != "observe":
        return fallback_mode
    if message is None:
        return fallback_mode
    run_mode = message.metadata.get("run_mode") if isinstance(message.metadata, dict) else None
    if isinstance(run_mode, str) and run_mode.strip():
        return run_mode.strip()
    return fallback_mode


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one worker.process() for stage development")
    parser.add_argument("--stage", required=True)
    parser.add_argument("--input-run-id", default="")
    parser.add_argument("--input-file", default="")
    parser.add_argument("--job-urls-file", default="")
    parser.add_argument("--job-platform", default="")
    parser.add_argument("--from-queue", action="store_true")
    parser.add_argument("--queue-message-id", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--mode", choices=["observe", "apply", "apply-dryrun"], default="observe")
    return parser.parse_args()


async def amain() -> int:
    args = _parse_args()

    cfg = AppConfig()
    logger = logging.getLogger("autorole.workers.devrun")
    logger.setLevel(logging.INFO)
    artifacts_root = Path(cfg.base_dir).expanduser() / "logs" / "runs"
    artifacts_root.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(Path(cfg.db_path).expanduser()) as db:
        await init_db(db)
        repo = JobRepository(db)
        input_q, reply_q = _stage_to_queues(args.stage)
        selected_message: Message | None = None
        queue = SqliteQueueBackend(db)

        if args.from_queue:
            selected_message = await _claim_queue_message(
                db,
                input_q,
                message_id=args.queue_message_id,
            )
            if selected_message is None:
                selected = args.queue_message_id.strip()
                suffix = f" message_id={selected}" if selected else ""
                raise ValueError(f"No queued message found in queue {input_q}{suffix}")
            payload = selected_message.payload
        else:
            payload = await _load_payload(args, repo)

        effective_mode = _message_mode(selected_message, args.mode)
        if effective_mode == "observe" and args.stage in {"session", "form_intelligence", "llm_field_completer", "form_submission"}:
            print(f"[warn] observe mode skips stage '{args.stage}'")
            return 0

        if args.dry_run:
            print(f"=== devrun: {args.stage} ===")
            print(f"run_id:   {(selected_message.run_id if selected_message is not None else payload.get('run_id', 'devrun-seed'))}")
            print(f"input_q:  {input_q}")
            print(f"reply_q:  {reply_q}")
            print("mode:     dry-run")
            return 0

        worker, playwright, closable, page = await _build_worker(
            args.stage,
            cfg,
            repo,
            logger,
            artifacts_root,
            headless=args.headless,
            search_platforms=_effective_search_platforms(cfg, payload, args.stage),
            payload=payload,
        )

        await _prepare_stage_page(args.stage, payload, page)

        if selected_message is not None:
            msg = selected_message
            metadata = dict(msg.metadata or {})
            metadata["run_mode"] = effective_mode
            if args.stage == "form_submission" and effective_mode == "apply-dryrun":
                metadata["dryrun_stop_after_submit"] = True
            msg.metadata = metadata
        else:
            msg = _build_message(
                payload,
                input_q,
                reply_q,
                stage=args.stage,
                mode=effective_mode,
            )
            await queue.enqueue(input_q, msg)
            pulled = await queue.pull(input_q)
            assert pulled is not None
            msg = pulled

        try:
            await worker.process(queue, msg)
            await asyncio.sleep(10)  # Wait for any async finalization in worker
        finally:
            try:
                if closable is not None:
                    await closable.close()
                if playwright is not None:
                    await playwright.stop()
            except Exception:
                pass

        outputs = await _list_queue_messages(db, reply_q, run_id=msg.run_id)
        dlq_messages = await _list_queue_messages(db, DEAD_LETTER_Q, run_id=msg.run_id)
        loop_q = FORM_INTEL_Q if args.stage == "form_submission" else input_q
        loop_messages = await _list_queue_messages(db, loop_q, run_id=msg.run_id)

        decision = "pass"
        if dlq_messages:
            decision = "block"
        elif loop_messages and (args.stage == "form_submission" or loop_messages[0].attempt > 1):
            decision = "loop"

        print(f"=== devrun: {args.stage} ===")
        print(f"run_id:   {msg.run_id}")
        print(f"decision: {decision}")
        if selected_message is not None:
            print(f"source:   queue:{input_q}")
        print("")
        print(f"[output queue: {reply_q}]")
        if not outputs:
            print("  empty")
        else:
            print(f"  count: {len(outputs)}")
            for index, out in enumerate(outputs, start=1):
                keys = ", ".join(sorted(out.payload.keys())) if isinstance(out.payload, dict) else type(out.payload).__name__
                print(f"  [{index}] message_id: {out.message_id}")
                print(f"      run_id: {out.run_id}")
                print(f"      payload keys: {keys}")
                if isinstance(out.payload, dict):
                    summary = _listing_summary(out.payload)
                    if summary is not None:
                        print("      success listing:")
                        _print_json_block("        ", summary)
                    print("      next-stage input payload:")
                    _print_json_block("        ", out.payload)

        print("")
        print("[dead_letter_q]")
        if not dlq_messages:
            print("  empty")
        else:
            print(f"  count: {len(dlq_messages)}")
            for dlq in dlq_messages:
                print(f"  message_id: {dlq.message_id}")

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
