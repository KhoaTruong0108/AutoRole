from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import typer

from autorole_next.app import build_runner, build_store
from autorole_next._snapflow import RunStatus
from autorole_next.payloads import ExplorationInput, ListingPayload, ListingSeed
from autorole_next.seeders.exploring import ExploringSeeder
from autorole_next.tui.run import launch_tui

app = typer.Typer(help="AutoRole Next command line interface")
run_app = typer.Typer(help="Seed and run autorole_next workflows")
app.add_typer(run_app, name="run")


def _slug_from_job_url(job_url: str) -> str:
    path = urlsplit(job_url).path.rstrip("/")
    slug = PurePosixPath(path).name.strip()
    return slug or "manual-job"


def _infer_platform(job_url: str, platform_hint: str | None) -> str:
    if platform_hint and platform_hint.strip():
        return platform_hint.strip().lower()
    hostname = urlsplit(job_url).netloc.lower()
    if "workday" in hostname:
        return "workday"
    if "greenhouse" in hostname:
        return "greenhouse"
    if "lever" in hostname:
        return "lever"
    return hostname.split(".")[0] or "manual"


def _infer_company_name(job_url: str, explicit_company_name: str) -> str:
    if explicit_company_name.strip():
        return explicit_company_name.strip()
    hostname = urlsplit(job_url).netloc.split(":", maxsplit=1)[0]
    primary = hostname.split(".")[0].replace("-", " ").replace("_", " ").strip()
    return primary.title() or "Unknown Company"


def _infer_job_title(job_url: str, explicit_job_title: str) -> str:
    if explicit_job_title.strip():
        return explicit_job_title.strip()
    slug = _slug_from_job_url(job_url).replace("-", " ").replace("_", " ").strip()
    return slug.title() or "Unknown Role"


def _load_metadata(metadata_json: str) -> dict[str, Any]:
    try:
        decoded = json.loads(metadata_json or "{}")
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"Invalid metadata JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise typer.BadParameter("--metadata-json must decode to a JSON object")
    return decoded


def _manual_listing_seed(
    job_url: str,
    *,
    platform_hint: str | None,
    company_name: str,
    job_title: str,
    source_name: str,
    source_metadata: dict[str, Any],
) -> ListingSeed:
    slug = _slug_from_job_url(job_url)
    listing = ListingPayload(
        job_url=job_url,
        apply_url=job_url, #f"{job_url.rstrip('/')}/apply",
        company_name=_infer_company_name(job_url, company_name),
        external_job_id=slug,
        job_title=_infer_job_title(job_url, job_title),
        platform=_infer_platform(job_url, platform_hint),
    )
    return ListingSeed(
        listing=listing,
        source_name=source_name,
        source_metadata=source_metadata,
    )


async def _run_seed_command(
    *,
    db_path: str,
    job_url: str,
    job_urls_file: str,
    platform_hint: str | None,
    company_name: str,
    job_title: str,
    source_name: str,
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    runner = build_runner(db_path)
    store = build_store(db_path)

    async def resolve(url: str, resolved_platform_hint: str | None) -> ListingSeed:
        return _manual_listing_seed(
            url,
            platform_hint=resolved_platform_hint,
            company_name=company_name,
            job_title=job_title,
            source_name=source_name,
            source_metadata=metadata,
        )

    seeder = ExploringSeeder(runner, store, job_url_resolver=resolve)
    seeded = await seeder.seed(
        ExplorationInput(
            job_url=job_url,
            job_urls_file=job_urls_file,
            platform_hint=platform_hint,
            metadata=metadata,
        )
    )
    return [item.model_dump(mode="json") for item in seeded]


async def _count_running_for_stage(store: Any, stage: str) -> int:
    running = await store.list_runs(status=RunStatus.RUNNING, limit=1000, offset=0)
    count = 0
    for run in running:
        ctx = await store.load_context(run.correlation_id)
        if ctx is not None and ctx.current_stage == stage:
            count += 1
    return count


async def _run_stage_worker(
    *,
    db_path: str,
    stage: str,
    watch: bool,
    poll_seconds: float,
    idle_rounds: int,
    max_seconds: int,
) -> dict[str, Any]:
    runner = build_runner(db_path)
    store = build_store(db_path)

    await runner.start(stage_ids=[stage])
    started_at = time.monotonic()
    stable_idle_rounds = 0

    try:
        while True:
            queue_depth = await runner._topology.queue_backend.depth(stage)  # noqa: SLF001 - used for CLI worker visibility
            running_count = await _count_running_for_stage(store, stage)

            if not watch:
                if queue_depth == 0 and running_count == 0:
                    stable_idle_rounds += 1
                else:
                    stable_idle_rounds = 0
                if stable_idle_rounds >= max(1, idle_rounds):
                    return {
                        "stage": stage,
                        "status": "drained",
                        "queue_depth": queue_depth,
                        "running": running_count,
                        "elapsed_seconds": round(time.monotonic() - started_at, 3),
                    }

                if max_seconds > 0 and (time.monotonic() - started_at) >= max_seconds:
                    return {
                        "stage": stage,
                        "status": "timeout",
                        "queue_depth": queue_depth,
                        "running": running_count,
                        "elapsed_seconds": round(time.monotonic() - started_at, 3),
                    }

            await asyncio.sleep(max(0.05, poll_seconds))
    finally:
        await runner.shutdown(mode="hard")


@run_app.command("seed")
def seed(
    db: str = typer.Option("tmp/autorole-next.db", "--db", help="SQLite database path."),
    job_url: str = typer.Option("", "--job-url", help="Seed a single job URL."),
    job_urls_file: str = typer.Option(
        "",
        "--job-urls-file",
        help="Path to a JSON list of job URLs or an object with a job_urls list.",
    ),
    platform_hint: str | None = typer.Option(None, "--platform", help="Platform hint for manual URL seeding."),
    company_name: str = typer.Option("", "--company-name", help="Fallback company name for manual URL seeding."),
    job_title: str = typer.Option("", "--job-title", help="Fallback job title for manual URL seeding."),
    source_name: str = typer.Option("manual-cli", "--source-name", help="Source name recorded in listings."),
    metadata_json: str = typer.Option("{}", "--metadata-json", help="JSON object stored as source metadata."),
) -> None:
    if not job_url.strip() and not job_urls_file.strip():
        raise typer.BadParameter("Provide either --job-url or --job-urls-file")

    metadata = _load_metadata(metadata_json)
    results = asyncio.run(
        _run_seed_command(
            db_path=db,
            job_url=job_url,
            job_urls_file=job_urls_file,
            platform_hint=platform_hint,
            company_name=company_name,
            job_title=job_title,
            source_name=source_name,
            metadata=metadata,
        )
    )
    typer.echo(json.dumps(results, indent=2))


@run_app.command("stage")
def run_stage(
    stage: str = typer.Option("scoring", "--stage", help="Stage id to run independently."),
    db: str = typer.Option("tmp/autorole-next.db", "--db", help="SQLite database path."),
    watch: bool = typer.Option(False, "--watch", help="Keep worker alive until interrupted."),
    poll_seconds: float = typer.Option(0.2, "--poll-seconds", help="Polling interval while observing queue drain."),
    idle_rounds: int = typer.Option(5, "--idle-rounds", help="Consecutive idle checks before considering stage drained."),
    max_seconds: int = typer.Option(120, "--max-seconds", help="Maximum seconds to wait before returning timeout in non-watch mode."),
) -> None:
    result = asyncio.run(
        _run_stage_worker(
            db_path=db,
            stage=stage,
            watch=watch,
            poll_seconds=poll_seconds,
            idle_rounds=idle_rounds,
            max_seconds=max_seconds,
        )
    )
    typer.echo(json.dumps(result, indent=2))


@app.command("tui")
def tui(
    db: str = typer.Option("tmp/autorole-next.db", "--db", help="SQLite database path."),
) -> None:
    launch_tui(db)


def main() -> None:
    app()


if __name__ == "__main__":
    main()