#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiosqlite

from autorole.config import AppConfig
from autorole.context import JobApplicationContext
from autorole.db.repository import JobRepository
from autorole.gates.best_fit import BestFitGate
from autorole.integrations.credentials import CredentialStore
from autorole.integrations.llm import AnthropicLLMClient, OllamaLLMClient, OpenAILLMClient
from autorole.integrations.renderer import PandocRenderer, WeasyPrintRenderer
from autorole.integrations.scrapers.indeed import IndeedScraper
from autorole.integrations.scrapers.linkedin import LinkedInScraper
from autorole.integrations.scrapers.url_posting import GenericJobPostingExtractor
from autorole.pipeline import inject_loop_metadata_from_gate_reason
from autorole.stages.concluding import ConcludingStage
from autorole.stages.exploring import ExploringStage, ManualUrlExploringStage
from autorole.stages.form_intelligence import FormIntelligenceStage
from autorole.stages.form_submission import FormSubmissionStage
from autorole.stages.packaging import PackagingStage
from autorole.stages.scoring import ScoringStage
from autorole.stages.session import SessionStage
from autorole.stages.tailoring import TailoringStage


@dataclass
class Message:
    run_id: str
    payload: dict[str, Any]
    metadata: dict[str, Any]
    attempt: int = 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run AutoRole stages with real integrations (no mocks)."
    )
    parser.add_argument(
        "--mode",
        choices=["observe", "apply"],
        default="observe",
        help="observe: stop before submit; apply: attempt real submission and concluding",
    )
    parser.add_argument(
        "--platforms",
        default="linkedin,indeed",
        help="Comma-separated platforms (supported: linkedin, indeed)",
    )
    parser.add_argument(
        "--job-url",
        default="",
        help="Manual mode: single job posting URL to process",
    )
    parser.add_argument(
        "--job-platform",
        default="",
        help="Optional manual platform hint (e.g. linkedin, indeed, custom)",
    )
    parser.add_argument(
        "--keywords",
        default="",
        help="Comma-separated keywords for search",
    )
    parser.add_argument(
        "--location",
        default="",
        help="Search location",
    )
    parser.add_argument(
        "--max-listings",
        type=int,
        default=1,
        help="Max number of listings to process from exploration result",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run browser in headless mode (default is headed for easier debugging)",
    )
    return parser.parse_args()


async def init_db(db: aiosqlite.Connection) -> None:
    migration = Path("src/autorole/db/migrations/001_domain.sql")
    sql = migration.read_text(encoding="utf-8")
    await db.executescript(sql)
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


def _parse_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


async def run_listing(
    ctx: JobApplicationContext,
    config: AppConfig,
    repo: JobRepository,
    stages: dict[str, Any],
    mode: str,
) -> None:
    print(f"\n=== RUN {ctx.run_id} ===")
    await repo.upsert_listing(ctx.listing, ctx.run_id)
    print("[ok] exploring -> listing saved")

    metadata: dict[str, Any] = {}
    attempt = 1

    while True:
        scoring = stages["scoring"]
        tailoring = stages["tailoring"]
        gate = stages["gate"]

        score_result = await scoring.execute(
            Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata=metadata, attempt=attempt)
        )
        if not score_result.success:
            print(f"[fail] scoring: {score_result.error}")
            return
        ctx = JobApplicationContext.model_validate(score_result.output)
        await repo.upsert_score(ctx.run_id, ctx.score, attempt=attempt)
        print(f"[ok] scoring -> overall_score={ctx.score.overall_score:.3f} (attempt {attempt})")

        tailor_result = await tailoring.execute(
            Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata=metadata, attempt=attempt)
        )
        if not tailor_result.success:
            print(f"[fail] tailoring: {tailor_result.error}")
            return
        ctx = JobApplicationContext.model_validate(tailor_result.output)
        await repo.upsert_tailored(ctx.run_id, ctx.tailored)
        print(
            "[ok] tailoring -> "
            f"degree={ctx.tailored.tailoring_degree} file={ctx.tailored.file_path}"
        )

        gate_result = gate.evaluate(
            SimpleNamespace(output=ctx.model_dump()),
            Message(run_id=ctx.run_id, payload={}, metadata=metadata, attempt=attempt),
        )
        decision = getattr(gate_result.decision, "value", str(gate_result.decision))

        if decision == "loop":
            metadata = inject_loop_metadata_from_gate_reason(metadata, gate_result.reason)
            attempt += 1
            print(f"[loop] best_fit -> {gate_result.reason}")
            continue

        if decision == "block":
            print(f"[block] best_fit -> {gate_result.reason}")
            return

        print("[ok] best_fit -> pass")
        break

    packaging = stages["packaging"]
    session = stages["session"]
    form_intelligence = stages["form_intelligence"]

    packaging_result = await packaging.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata={}))
    if not packaging_result.success:
        print(f"[fail] packaging: {packaging_result.error}")
        return
    ctx = JobApplicationContext.model_validate(packaging_result.output)
    print(f"[ok] packaging -> pdf={ctx.packaged.pdf_path}")

    session_result = await session.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata={}))
    if not session_result.success:
        print(f"[fail] session: {session_result.error}")
        return
    ctx = JobApplicationContext.model_validate(session_result.output)
    await repo.upsert_session(ctx.run_id, ctx.session)
    print(f"[ok] session -> authenticated={ctx.session.authenticated}")

    intel_result = await form_intelligence.execute(
        Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata={})
    )
    if not intel_result.success:
        print(f"[fail] form_intelligence: {intel_result.error}")
        return
    ctx = JobApplicationContext.model_validate(intel_result.output)
    print("[ok] form_intelligence -> form extracted and filled")

    if mode == "observe":
        print("[stop] observe mode enabled; skipping submission and concluding")
        return

    form_submission = stages["form_submission"]
    concluding = stages["concluding"]

    submit_result = await form_submission.execute(
        Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata={})
    )
    if not submit_result.success:
        print(f"[fail] form_submission: {submit_result.error}")
        return
    ctx = JobApplicationContext.model_validate(submit_result.output)
    print(
        "[ok] form_submission -> "
        f"status={ctx.applied.submission_status} confirmed={ctx.applied.submission_confirmed}"
    )

    concluding_result = await concluding.execute(
        Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata={})
    )
    if not concluding_result.success:
        print(f"[fail] concluding: {concluding_result.error}")
        return

    print("[ok] concluding -> job application persisted")
    print(
        f"[done] run_id={ctx.run_id} score={ctx.score.overall_score:.3f} "
        f"tailoring_degree={ctx.tailored.tailoring_degree}"
    )


async def amain() -> int:
    args = parse_args()
    config = AppConfig()

    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        print("Playwright is required for real runs.")
        print("Install with: python -m pip install playwright")
        print("Then install browser with: python -m playwright install chromium")
        print(f"Import error: {exc}")
        return 2

    base_dir = Path(config.base_dir).expanduser()
    resume_dir = Path(config.resume_dir).expanduser()
    db_path = Path(config.db_path).expanduser()

    base_dir.mkdir(parents=True, exist_ok=True)
    resume_dir.mkdir(parents=True, exist_ok=True)

    if not Path(config.master_resume).expanduser().exists():
        print(f"Missing master resume: {Path(config.master_resume).expanduser()}")
        return 2

    is_manual_url_mode = bool(args.job_url.strip())
    platforms = _parse_csv(args.platforms)

    search_config = config.search.model_dump()
    if platforms:
        search_config["platforms"] = platforms
    keywords = _parse_csv(args.keywords)
    if keywords:
        search_config["keywords"] = keywords
    if args.location:
        search_config["location"] = args.location

    if not is_manual_url_mode and not platforms:
        print("No platforms selected")
        return 2

    async with aiosqlite.connect(db_path) as db:
        await init_db(db)
        repo = JobRepository(db)

        llm_client = make_llm_client(config)
        renderer = make_renderer(config)

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=args.headless)
            browser_context = await browser.new_context()

            scrape_page = await browser_context.new_page()
            score_page = await browser_context.new_page()
            form_page = await browser_context.new_page()

            scrapers: dict[str, Any] = {}
            if "linkedin" in platforms:
                scrapers["linkedin"] = LinkedInScraper(scrape_page)
            if "indeed" in platforms:
                scrapers["indeed"] = IndeedScraper(scrape_page)

            if is_manual_url_mode:
                extractor = GenericJobPostingExtractor(scrape_page)
                platform_hint = args.job_platform.strip() or None
                exploring = ManualUrlExploringStage(config, extractor=extractor, platform_hint=platform_hint)
                seed_payload: dict[str, Any] = {"job_url": args.job_url.strip()}
                print("Starting exploration in manual URL mode...")
            else:
                exploring = ExploringStage(config, scrapers=scrapers)
                seed_payload = {"search_config": search_config}
                print("Starting exploration with real scrapers...")

            stages = {
                "scoring": ScoringStage(config, llm_client, score_page),
                "tailoring": TailoringStage(config, llm_client),
                "gate": BestFitGate(max_attempts=config.tailoring.max_attempts),
                "packaging": PackagingStage(config, renderer),
                "session": SessionStage(config, CredentialStore()),
                "form_intelligence": FormIntelligenceStage(config, llm_client, form_page),
                "form_submission": FormSubmissionStage(config, form_page),
                "concluding": ConcludingStage(config, repo),
            }

            seed = Message(
                run_id="seed",
                payload=seed_payload,
                metadata={"source": "real_runner"},
                attempt=1,
            )

            explore_result = await exploring.execute(seed)
            if not explore_result.success:
                print(f"Exploring failed: {explore_result.error}")
                await browser_context.close()
                await browser.close()
                return 1

            contexts: list[JobApplicationContext] = explore_result.output
            selected = contexts[: max(1, args.max_listings)]
            print(f"Exploring found {len(contexts)} listing(s); processing {len(selected)}")

            for listing_ctx in selected:
                await run_listing(listing_ctx, config, repo, stages, args.mode)

            await browser_context.close()
            await browser.close()

    print("\nRun completed.")
    print(f"DB: {db_path}")
    print("Use CLI to verify:")
    print("  PYTHONPATH=src python -m autorole.cli.main status")
    print("  PYTHONPATH=src python -m autorole.cli.main score <run_id>")
    print("  PYTHONPATH=src python -m autorole.cli.main diff <run_id>")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
