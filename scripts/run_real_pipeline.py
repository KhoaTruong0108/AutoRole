#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
	sys.path.insert(0, str(SRC_PATH))

from autorole.config import AppConfig
from autorole.job_pipeline import JobApplicationPipeline, RunConfig

STAGE_ORDER = [
	"exploring",
	"scoring",
	"tailoring",
	"packaging",
	"session",
	"form_intelligence",
	"form_submission",
	"concluding",
]

FROM_STAGE_ALIASES = {
	"form_intelligent": "form_intelligence",
}


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(
		description="Run AutoRole stages with real integrations (no mocks)."
	)
	parser.add_argument(
		"--mode",
		choices=["observe", "apply", "apply-dryrun"],
		default="observe",
		help=(
			"observe: stop before packaging; "
			"apply-dryrun: execute submit click then stop before concluding; "
			"apply: full flow including concluding"
		),
	)
	parser.add_argument(
		"--platforms",
		default="linkedin,indeed",
		help="Comma-separated platforms (examples: linkedin, indeed, lever, greenhouse)",
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
	parser.add_argument(
		"--resume-run-id",
		default="",
		help="Resume from a previously checkpointed run_id",
	)
	parser.add_argument(
		"--from-stage",
		choices=STAGE_ORDER + list(FROM_STAGE_ALIASES.keys()),
		default="",
		help="Force starting stage when resuming; if omitted, starts after last successful stage",
	)
	return parser.parse_args()


def _parse_csv(value: str) -> list[str]:
	return [part.strip() for part in value.split(",") if part.strip()]


def _normalize_from_stage(value: str) -> str:
	return FROM_STAGE_ALIASES.get(value, value)


async def amain() -> int:
	args = parse_args()
	config = AppConfig()
	run_config = RunConfig(
		mode=args.mode,
		platforms=_parse_csv(args.platforms),
		job_url=args.job_url,
		job_platform=args.job_platform,
		keywords=_parse_csv(args.keywords),
		location=args.location,
		max_listings=args.max_listings,
		headless=args.headless,
		resume_run_id=args.resume_run_id,
		from_stage=_normalize_from_stage(args.from_stage),
	)
	return await JobApplicationPipeline(config, run_config).run()


def main() -> None:
	raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
	main()
