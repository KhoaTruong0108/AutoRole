from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import aiosqlite

from autorole.config import AppConfig
from autorole.context import JobApplicationContext
from autorole.db.repository import JobRepository
from autorole.job_pipeline import _make_seed_message, _next_stage, _stage_to_queue, init_db
from autorole.queue import EXPLORING_Q, SqliteQueueBackend


async def amain() -> int:
    parser = argparse.ArgumentParser(description="Seed AutoRole queue with a new job request")
    parser.add_argument("--job-url", default="")
    parser.add_argument("--search", action="store_true")
    parser.add_argument("--resume-run-id", default="")
    parser.add_argument("--from-stage", default="")
    parser.add_argument("--max-listings", type=int, default=1)
    args = parser.parse_args()

    config = AppConfig()
    async with aiosqlite.connect(Path(config.db_path).expanduser()) as db:
        await init_db(db)
        backend = SqliteQueueBackend(db)
        repo = JobRepository(db)

        if args.resume_run_id.strip():
            checkpoint = await repo.get_checkpoint(args.resume_run_id.strip())
            if checkpoint is None:
                raise RuntimeError(f"No checkpoint found for run_id={args.resume_run_id.strip()}")
            last_stage, checkpoint_ctx = checkpoint
            resume_ctx = JobApplicationContext.model_validate(checkpoint_ctx)
            start_stage = args.from_stage or _next_stage(last_stage)
            if start_stage is None:
                return 0
            start_queue = _stage_to_queue(start_stage)
            seed = _make_seed_message(resume_ctx.run_id, resume_ctx.model_dump(mode="json"), start_queue)
            await backend.enqueue(start_queue, seed)
            return 0

        if args.job_url.strip():
            payload = {"job_url": args.job_url.strip(), "max_listings": args.max_listings}
        else:
            payload = {"search_config": config.search.model_dump(), "max_listings": args.max_listings}

        seed = _make_seed_message("seed", payload, EXPLORING_Q)
        await backend.enqueue(EXPLORING_Q, seed)

    return 0


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
