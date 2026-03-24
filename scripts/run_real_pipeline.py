#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, time, timezone
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiosqlite

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))

from autorole.config import AppConfig
from autorole.context import FormIntelligenceResult, JobApplicationContext, PackagedResume
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


def _next_stage(stage_name: str) -> str | None:
    try:
        idx = STAGE_ORDER.index(stage_name)
    except ValueError:
        return None
    next_idx = idx + 1
    if next_idx >= len(STAGE_ORDER):
        return None
    return STAGE_ORDER[next_idx]


def _build_resume_command(run_id: str, mode: str, from_stage: str) -> str:
    return (
        "PYTHONPATH=src python3 scripts/run_real_pipeline.py "
        f"--resume-run-id {run_id} --from-stage {from_stage} --mode {mode}"
    )


def _emit_resume_hint(logger: logging.Logger, run_id: str, mode: str, from_stage: str) -> None:
    cmd = _build_resume_command(run_id, mode, from_stage)
    print(f"[resume-cmd] {cmd}")
    logger.info("RESUME_COMMAND run_id=%s stage=%s cmd=%s", run_id, from_stage, cmd)


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
        choices=STAGE_ORDER,
        default="",
        help="Force starting stage when resuming; if omitted, starts after last successful stage",
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

    # Mirror errors to terminal so failures are visible immediately during local runs.
    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.ERROR)
    stderr_handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    logger.addHandler(stderr_handler)
    return logger, log_path


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _append_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(content)


def _record_stage_artifact(
    *,
    logger: logging.Logger,
    run_dir: Path,
    stage_name: str,
    filename: str,
    content: str,
) -> Path:
    artifact_path = run_dir / stage_name / filename
    _write_text(artifact_path, content)

    index_path = run_dir / "stage_outputs.md"
    rel_path = artifact_path.relative_to(run_dir)
    _append_text(index_path, f"- {stage_name}: {rel_path}\n")

    logger.info(
        "STAGE_ARTIFACT stage=%s path=%s",
        stage_name,
        artifact_path,
    )
    return artifact_path


async def _execute_stage(stage_name: str, stage: Any, msg: Message, logger: logging.Logger) -> Any | None:
    try:
        return await stage.execute(msg)
    except Exception:
        logger.exception("Unhandled exception in stage=%s run_id=%s", stage_name, msg.run_id)
        return None


async def run_listing(
    ctx: JobApplicationContext,
    config: AppConfig,
    repo: JobRepository,
    stages: dict[str, Any],
    mode: str,
    trace_logger: logging.Logger,
    artifacts_root: Path,
    start_stage: str = "exploring",
) -> None:
    _ = config
    print(f"\n=== RUN {ctx.run_id} ===")
    trace_logger.info("Starting listing run_id=%s mode=%s start_stage=%s", ctx.run_id, mode, start_stage)

    run_artifacts_dir = artifacts_root / ctx.run_id
    run_artifacts_dir.mkdir(parents=True, exist_ok=True)
    stage_index_path = run_artifacts_dir / "stage_outputs.md"
    if stage_index_path.exists() and start_stage != "exploring":
        _append_text(stage_index_path, f"\n## Resumed from {start_stage} at {datetime.now(timezone.utc).isoformat()}\n")
    else:
        _write_text(stage_index_path, f"# Stage Outputs for {ctx.run_id}\n\n")
    trace_logger.info("RUN_ARTIFACT_INDEX run_id=%s path=%s", ctx.run_id, stage_index_path)

    start_idx = STAGE_ORDER.index(start_stage)

    def should_run(stage_name: str) -> bool:
        return STAGE_ORDER.index(stage_name) >= start_idx

    listing_json = json.dumps(ctx.listing.model_dump(mode="json"), indent=2, ensure_ascii=False)
    _record_stage_artifact(
        logger=trace_logger,
        run_dir=run_artifacts_dir,
        stage_name="exploring",
        filename="listing.json",
        content=listing_json + "\n",
    )

    if should_run("exploring"):
        await repo.upsert_listing(ctx.listing, ctx.run_id)
        await repo.upsert_checkpoint(ctx.run_id, "exploring", ctx.model_dump(mode="json"))
        print("[ok] exploring -> listing saved")
    else:
        print(f"[resume] skipping exploring (start stage: {start_stage})")

    metadata: dict[str, Any] = {}
    attempt = 1

    if should_run("scoring") or should_run("tailoring"):
        while True:
            scoring = stages["scoring"]
            tailoring = stages["tailoring"]
            gate = stages["gate"]

            score_result = await _execute_stage(
                "scoring",
                scoring,
                Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata=metadata, attempt=attempt),
                trace_logger,
            )
            if score_result is None:
                print("[fail] scoring: unhandled exception (see trace log)")
                _emit_resume_hint(trace_logger, ctx.run_id, mode, "scoring")
                return
            if not score_result.success:
                trace_logger.error(
                    "Stage scoring failed run_id=%s attempt=%s error_type=%s error=%s",
                    ctx.run_id,
                    attempt,
                    getattr(score_result, "error_type", None),
                    score_result.error,
                )
                print(f"[fail] scoring: {score_result.error}")
                _record_stage_artifact(
                    logger=trace_logger,
                    run_dir=run_artifacts_dir,
                    stage_name="scoring",
                    filename=f"attempt_{attempt}_error.txt",
                    content=(
                        f"error_type={getattr(score_result, 'error_type', '')}\n"
                        f"error={score_result.error}\n"
                    ),
                )
                _emit_resume_hint(trace_logger, ctx.run_id, mode, "scoring")
                return

            ctx = JobApplicationContext.model_validate(score_result.output)
            score_payload = {
                "overall_score": ctx.score.overall_score,
                "criteria_scores": ctx.score.criteria_scores,
                "matched": ctx.score.matched,
                "mismatched": ctx.score.mismatched,
                "jd_breakdown": ctx.score.jd_breakdown,
            }
            _record_stage_artifact(
                logger=trace_logger,
                run_dir=run_artifacts_dir,
                stage_name="scoring",
                filename=f"attempt_{attempt}_summary.json",
                content=json.dumps(score_payload, indent=2, ensure_ascii=False) + "\n",
            )
            _record_stage_artifact(
                logger=trace_logger,
                run_dir=run_artifacts_dir,
                stage_name="scoring",
                filename=f"attempt_{attempt}_job_description.html",
                content=ctx.score.jd_html,
            )
            criteria_md = [
                f"# Scoring Criteria (attempt {attempt})",
                "",
                f"Overall score: {ctx.score.overall_score:.3f}",
                "",
                "## Criteria Scores",
                "",
                json.dumps(ctx.score.criteria_scores, indent=2, ensure_ascii=False),
                "",
                "## Job Requirements Breakdown",
                "",
                json.dumps(ctx.score.jd_breakdown, indent=2, ensure_ascii=False),
                "",
                "## Matched",
                "",
                json.dumps(ctx.score.matched, indent=2, ensure_ascii=False),
                "",
                "## Mismatched",
                "",
                json.dumps(ctx.score.mismatched, indent=2, ensure_ascii=False),
                "",
            ]
            _record_stage_artifact(
                logger=trace_logger,
                run_dir=run_artifacts_dir,
                stage_name="scoring",
                filename=f"attempt_{attempt}_criteria.md",
                content="\n".join(criteria_md),
            )
            await repo.upsert_score(ctx.run_id, ctx.score, attempt=attempt)
            await repo.upsert_checkpoint(ctx.run_id, "scoring", ctx.model_dump(mode="json"))
            print(f"[ok] scoring -> overall_score={ctx.score.overall_score:.3f} (attempt {attempt})")

            tailor_result = await _execute_stage(
                "tailoring",
                tailoring,
                Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata=metadata, attempt=attempt),
                trace_logger,
            )
            if tailor_result is None:
                print("[fail] tailoring: unhandled exception (see trace log)")
                _emit_resume_hint(trace_logger, ctx.run_id, mode, "tailoring")
                return
            if not tailor_result.success:
                trace_logger.error(
                    "Stage tailoring failed run_id=%s attempt=%s error_type=%s error=%s",
                    ctx.run_id,
                    attempt,
                    getattr(tailor_result, "error_type", None),
                    tailor_result.error,
                )
                print(f"[fail] tailoring: {tailor_result.error}")
                _record_stage_artifact(
                    logger=trace_logger,
                    run_dir=run_artifacts_dir,
                    stage_name="tailoring",
                    filename=f"attempt_{attempt}_error.txt",
                    content=(
                        f"error_type={getattr(tailor_result, 'error_type', '')}\n"
                        f"error={tailor_result.error}\n"
                    ),
                )
                _emit_resume_hint(trace_logger, ctx.run_id, mode, "tailoring")
                return

            ctx = JobApplicationContext.model_validate(tailor_result.output)
            tailoring_payload = {
                "tailoring_degree": ctx.tailored.tailoring_degree,
                "resume_id": ctx.tailored.resume_id,
                "parent_resume_id": ctx.tailored.parent_resume_id,
                "file_path": ctx.tailored.file_path,
                "diff_summary": ctx.tailored.diff_summary,
            }
            _record_stage_artifact(
                logger=trace_logger,
                run_dir=run_artifacts_dir,
                stage_name="tailoring",
                filename=f"attempt_{attempt}_summary.json",
                content=json.dumps(tailoring_payload, indent=2, ensure_ascii=False) + "\n",
            )
            _record_stage_artifact(
                logger=trace_logger,
                run_dir=run_artifacts_dir,
                stage_name="tailoring",
                filename=f"attempt_{attempt}_resume_diff.md",
                content=(
                    f"# Resume Diff (attempt {attempt})\n\n"
                    f"Tailoring degree: {ctx.tailored.tailoring_degree}\n\n"
                    f"Source file: {ctx.tailored.file_path}\n\n"
                    f"## Diff Summary\n\n{ctx.tailored.diff_summary}\n"
                ),
            )
            await repo.upsert_tailored(ctx.run_id, ctx.tailored)
            await repo.upsert_checkpoint(ctx.run_id, "tailoring", ctx.model_dump(mode="json"))
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
                trace_logger.info("best_fit loop run_id=%s reason=%s", ctx.run_id, gate_result.reason)
                print(f"[loop] best_fit -> {gate_result.reason}")
                continue
            if decision == "block":
                trace_logger.warning("best_fit block run_id=%s reason=%s", ctx.run_id, gate_result.reason)
                print(f"[block] best_fit -> {gate_result.reason}")
                return

            print("[ok] best_fit -> pass")
            break
    else:
        print(f"[resume] skipping scoring/tailoring/best_fit (start stage: {start_stage})")

    if mode == "observe":
        print("[stop] observe mode enabled; skipping packaging, session, and submission stages")
        return

    packaging = stages["packaging"]
    session = stages["session"]
    form_intelligence = stages["form_intelligence"]
    form_submission = stages["form_submission"]
    concluding = stages["concluding"]

    if should_run("packaging"):
        packaging_result = await _execute_stage(
            "packaging",
            packaging,
            Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata={}),
            trace_logger,
        )
        if packaging_result is None:
            print("[fail] packaging: unhandled exception (see trace log)")
            _emit_resume_hint(trace_logger, ctx.run_id, mode, "packaging")
            return
        if not packaging_result.success:
            trace_logger.error(
                "Stage packaging failed run_id=%s error_type=%s error=%s",
                ctx.run_id,
                getattr(packaging_result, "error_type", None),
                packaging_result.error,
            )
            if mode == "apply-dryrun" and ctx.tailored is not None:
                fallback_packaged = PackagedResume(
                    resume_id=ctx.tailored.resume_id,
                    pdf_path=ctx.tailored.file_path,
                    packaged_at=datetime.now(timezone.utc),
                )
                ctx = ctx.model_copy(update={"packaged": fallback_packaged})
                print(
                    f"[warn] packaging failed in apply-dryrun mode; {packaging_result.error} "
                    "falling back to tailored markdown for upload"
                )
                _record_stage_artifact(
                    logger=trace_logger,
                    run_dir=run_artifacts_dir,
                    stage_name="packaging",
                    filename="error.txt",
                    content=(
                        f"error_type={getattr(packaging_result, 'error_type', '')}\n"
                        f"error={packaging_result.error}\n"
                        "fallback=tailored_markdown\n"
                    ),
                )
            else:
                print(f"[fail] packaging: {packaging_result.error}")
                _record_stage_artifact(
                    logger=trace_logger,
                    run_dir=run_artifacts_dir,
                    stage_name="packaging",
                    filename="error.txt",
                    content=(
                        f"error_type={getattr(packaging_result, 'error_type', '')}\n"
                        f"error={packaging_result.error}\n"
                    ),
                )
                _emit_resume_hint(trace_logger, ctx.run_id, mode, "packaging")
                return
        else:
            ctx = JobApplicationContext.model_validate(packaging_result.output)
            _record_stage_artifact(
                logger=trace_logger,
                run_dir=run_artifacts_dir,
                stage_name="packaging",
                filename="output.json",
                content=json.dumps(ctx.packaged.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
            )
            print(f"[ok] packaging -> pdf={ctx.packaged.pdf_path}")
        await repo.upsert_checkpoint(ctx.run_id, "packaging", ctx.model_dump(mode="json"))
    else:
        print(f"[resume] skipping packaging (start stage: {start_stage})")

    if should_run("session"):
        session_result = await _execute_stage(
            "session",
            session,
            Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata={}),
            trace_logger,
        )
        if session_result is None:
            print("[fail] session: unhandled exception (see trace log)")
            _emit_resume_hint(trace_logger, ctx.run_id, mode, "session")
            return
        if not session_result.success:
            trace_logger.error(
                "Stage session failed run_id=%s error_type=%s error=%s",
                ctx.run_id,
                getattr(session_result, "error_type", None),
                session_result.error,
            )
            print(f"[fail] session: {session_result.error}")
            _record_stage_artifact(
                logger=trace_logger,
                run_dir=run_artifacts_dir,
                stage_name="session",
                filename="error.txt",
                content=(
                    f"error_type={getattr(session_result, 'error_type', '')}\n"
                    f"error={session_result.error}\n"
                ),
            )
            _emit_resume_hint(trace_logger, ctx.run_id, mode, "session")
            return
        ctx = JobApplicationContext.model_validate(session_result.output)
        _record_stage_artifact(
            logger=trace_logger,
            run_dir=run_artifacts_dir,
            stage_name="session",
            filename="output.json",
            content=json.dumps(ctx.session.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        )
        await repo.upsert_session(ctx.run_id, ctx.session)
        await repo.upsert_checkpoint(ctx.run_id, "session", ctx.model_dump(mode="json"))
        print(f"[ok] session -> authenticated={ctx.session.authenticated}")
    else:
        print(f"[resume] skipping session (start stage: {start_stage})")

    if should_run("form_intelligence"):
        intel_result = await _execute_stage(
            "form_intelligence",
            form_intelligence,
            Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata={}),
            trace_logger,
        )
        if intel_result is None:
            print("[fail] form_intelligence: unhandled exception (see trace log)")
            _emit_resume_hint(trace_logger, ctx.run_id, mode, "form_intelligence")
            return
        if not intel_result.success:
            trace_logger.error(
                "Stage form_intelligence failed run_id=%s error_type=%s error=%s",
                ctx.run_id,
                getattr(intel_result, "error_type", None),
                intel_result.error,
            )
            if mode == "apply-dryrun":
                fallback_intel = FormIntelligenceResult(
                    questionnaire=[],
                    form_json_filled={"fields": []},
                    generated_at=datetime.now(timezone.utc),
                )
                ctx = ctx.model_copy(update={"form_intelligence": fallback_intel})
                print(
                    f"[warn] form_intelligence failed in apply-dryrun mode; {intel_result.error} "
                    "continuing with empty form payload"
                )
                _record_stage_artifact(
                    logger=trace_logger,
                    run_dir=run_artifacts_dir,
                    stage_name="form_intelligence",
                    filename="error.txt",
                    content=(
                        f"error_type={getattr(intel_result, 'error_type', '')}\n"
                        f"error={intel_result.error}\n"
                        "fallback=empty_form_payload\n"
                    ),
                )
                _record_stage_artifact(
                    logger=trace_logger,
                    run_dir=run_artifacts_dir,
                    stage_name="form_intelligence",
                    filename="answered_form.md",
                    content=(
                        "# Answered Form\n\n"
                        "Form intelligence failed; fallback empty payload was used in apply-dryrun mode.\n\n"
                        "## Questionnaire\n\n[]\n\n"
                        "## Filled Form JSON\n\n{\n  \"fields\": []\n}\n"
                    ),
                )
            else:
                print(f"[fail] form_intelligence: {intel_result.error}")
                _record_stage_artifact(
                    logger=trace_logger,
                    run_dir=run_artifacts_dir,
                    stage_name="form_intelligence",
                    filename="error.txt",
                    content=(
                        f"error_type={getattr(intel_result, 'error_type', '')}\n"
                        f"error={intel_result.error}\n"
                    ),
                )
                _emit_resume_hint(trace_logger, ctx.run_id, mode, "form_intelligence")
                return
        else:
            ctx = JobApplicationContext.model_validate(intel_result.output)
            _record_stage_artifact(
                logger=trace_logger,
                run_dir=run_artifacts_dir,
                stage_name="form_intelligence",
                filename="questionnaire.json",
                content=json.dumps(ctx.form_intelligence.questionnaire, indent=2, ensure_ascii=False) + "\n",
            )
            _record_stage_artifact(
                logger=trace_logger,
                run_dir=run_artifacts_dir,
                stage_name="form_intelligence",
                filename="form_json_filled.json",
                content=json.dumps(ctx.form_intelligence.form_json_filled, indent=2, ensure_ascii=False) + "\n",
            )
            md_lines = [
                "# Answered Form",
                "",
                "## Questionnaire",
                "",
                json.dumps(ctx.form_intelligence.questionnaire, indent=2, ensure_ascii=False),
                "",
                "## Filled Form JSON",
                "",
                json.dumps(ctx.form_intelligence.form_json_filled, indent=2, ensure_ascii=False),
                "",
            ]
            _record_stage_artifact(
                logger=trace_logger,
                run_dir=run_artifacts_dir,
                stage_name="form_intelligence",
                filename="answered_form.md",
                content="\n".join(md_lines),
            )
            print("[ok] form_intelligence -> form extracted and filled")
        await repo.upsert_checkpoint(ctx.run_id, "form_intelligence", ctx.model_dump(mode="json"))
    else:
        print(f"[resume] skipping form_intelligence (start stage: {start_stage})")

    if should_run("form_submission"):
        submit_result = await _execute_stage(
            "form_submission",
            form_submission,
            Message(
                run_id=ctx.run_id,
                payload=ctx.model_dump(),
                metadata={"dryrun_stop_after_submit": mode == "apply-dryrun"},
            ),
            trace_logger,
        )
        if submit_result is None:
            print("[fail] form_submission: unhandled exception (see trace log)")
            _emit_resume_hint(trace_logger, ctx.run_id, mode, "form_submission")
            return
        if not submit_result.success:
            trace_logger.error(
                "Stage form_submission failed run_id=%s error_type=%s error=%s",
                ctx.run_id,
                getattr(submit_result, "error_type", None),
                submit_result.error,
            )
            print(f"[fail] form_submission: {submit_result.error}")
            _record_stage_artifact(
                logger=trace_logger,
                run_dir=run_artifacts_dir,
                stage_name="form_submission",
                filename="error.txt",
                content=(
                    f"error_type={getattr(submit_result, 'error_type', '')}\n"
                    f"error={submit_result.error}\n"
                ),
            )
            _emit_resume_hint(trace_logger, ctx.run_id, mode, "form_submission")
            return
        ctx = JobApplicationContext.model_validate(submit_result.output)
        _record_stage_artifact(
            logger=trace_logger,
            run_dir=run_artifacts_dir,
            stage_name="form_submission",
            filename="output.json",
            content=json.dumps(ctx.applied.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n",
        )
        _record_stage_artifact(
            logger=trace_logger,
            run_dir=run_artifacts_dir,
            stage_name="form_submission",
            filename="field_fill_report.json",
            content=json.dumps(ctx.applied.fill_report, indent=2, ensure_ascii=False) + "\n",
        )
        print(
            "[ok] form_submission -> "
            f"status={ctx.applied.submission_status} confirmed={ctx.applied.submission_confirmed}"
        )
        await repo.upsert_checkpoint(ctx.run_id, "form_submission", ctx.model_dump(mode="json"))
    else:
        print(f"[resume] skipping form_submission (start stage: {start_stage})")

    if mode == "apply-dryrun":
        print("[stop] apply-dryrun mode enabled; completed flow with submit click skipped")
        return

    if should_run("concluding"):
        concluding_result = await _execute_stage(
            "concluding",
            concluding,
            Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata={}),
            trace_logger,
        )
        if concluding_result is None:
            print("[fail] concluding: unhandled exception (see trace log)")
            _emit_resume_hint(trace_logger, ctx.run_id, mode, "concluding")
            return
        if not concluding_result.success:
            trace_logger.error(
                "Stage concluding failed run_id=%s error_type=%s error=%s",
                ctx.run_id,
                getattr(concluding_result, "error_type", None),
                concluding_result.error,
            )
            print(f"[fail] concluding: {concluding_result.error}")
            _record_stage_artifact(
                logger=trace_logger,
                run_dir=run_artifacts_dir,
                stage_name="concluding",
                filename="error.txt",
                content=(
                    f"error_type={getattr(concluding_result, 'error_type', '')}\n"
                    f"error={concluding_result.error}\n"
                ),
            )
            _emit_resume_hint(trace_logger, ctx.run_id, mode, "concluding")
            return
        _record_stage_artifact(
            logger=trace_logger,
            run_dir=run_artifacts_dir,
            stage_name="concluding",
            filename="output.txt",
            content="Concluding stage completed successfully.\n",
        )
        await repo.upsert_checkpoint(ctx.run_id, "concluding", ctx.model_dump(mode="json"))
    else:
        print(f"[resume] skipping concluding (start stage: {start_stage})")

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
    stage_outputs_root = base_dir / "logs" / "runs"

    base_dir.mkdir(parents=True, exist_ok=True)
    trace_logger, trace_log_path = _configure_trace_logger(base_dir)
    trace_logger.info("Starting real pipeline run mode=%s", args.mode)
    stage_outputs_root.mkdir(parents=True, exist_ok=True)
    trace_logger.info("STAGE_OUTPUTS_ROOT path=%s", stage_outputs_root)
    print(f"Trace log: {trace_log_path}")
    resume_dir.mkdir(parents=True, exist_ok=True)

    if not Path(config.master_resume).expanduser().exists():
        print(f"Missing master resume: {Path(config.master_resume).expanduser()}")
        return 2

    is_manual_url_mode = bool(args.job_url.strip())
    is_resume_mode = bool(args.resume_run_id.strip())
    platforms = _parse_csv(args.platforms)

    search_config = config.search.model_dump()
    if platforms:
        search_config["platforms"] = platforms
    keywords = _parse_csv(args.keywords)
    if keywords:
        search_config["keywords"] = keywords
    if args.location:
        search_config["location"] = args.location

    if not is_resume_mode and not is_manual_url_mode and not platforms:
        print("No platforms selected")
        return 2

    try:
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

                if is_resume_mode:
                    exploring = ExploringStage(config, scrapers=scrapers)
                    seed_payload = {"search_config": search_config}
                elif is_manual_url_mode:
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
                    "form_intelligence": FormIntelligenceStage(
                        config,
                        llm_client,
                        form_page,
                        use_random_questionnaire_answers=args.mode in {"observe", "apply-dryrun"},
                    ),
                    "form_submission": FormSubmissionStage(config, form_page),
                    "concluding": ConcludingStage(config, repo),
                }

                start_stage = "exploring"
                if is_resume_mode:
                    resume_run_id = args.resume_run_id.strip()
                    checkpoint = await repo.get_checkpoint(resume_run_id)
                    if checkpoint is None:
                        print(f"No checkpoint found for run_id={resume_run_id}")
                        await browser_context.close()
                        await browser.close()
                        return 1

                    last_success_stage, checkpoint_ctx = checkpoint
                    resume_ctx = JobApplicationContext.model_validate(checkpoint_ctx)
                    if args.from_stage:
                        start_stage = args.from_stage
                    else:
                        next_stage = _next_stage(last_success_stage)
                        if next_stage is None:
                            print(
                                f"Checkpoint for run_id={resume_run_id} already completed at stage={last_success_stage}"
                            )
                            await browser_context.close()
                            await browser.close()
                            return 0
                        start_stage = next_stage

                    selected = [resume_ctx]
                    print(
                        f"Resuming run_id={resume_run_id} from stage={start_stage} "
                        f"(last successful stage: {last_success_stage})"
                    )
                else:
                    seed = Message(
                        run_id="seed",
                        payload=seed_payload,
                        metadata={"source": "real_runner"},
                        attempt=1,
                    )

                    explore_result = await _execute_stage("exploring", exploring, seed, trace_logger)
                    if explore_result is None:
                        print("Exploring failed: unhandled exception (see trace log)")
                        _emit_resume_hint(trace_logger, "seed", args.mode, "exploring")
                        await browser_context.close()
                        await browser.close()
                        return 1
                    if not explore_result.success:
                        trace_logger.error(
                            "Stage exploring failed error_type=%s error=%s",
                            getattr(explore_result, "error_type", None),
                            explore_result.error,
                        )
                        print(f"Exploring failed: {explore_result.error}")
                        _emit_resume_hint(trace_logger, "seed", args.mode, "exploring")
                        await browser_context.close()
                        await browser.close()
                        return 1

                    contexts: list[JobApplicationContext] = explore_result.output
                    selected = contexts[: max(1, args.max_listings)]
                    print(f"Exploring found {len(contexts)} listing(s); processing {len(selected)}")

                for listing_ctx in selected:
                    await run_listing(
                        listing_ctx,
                        config,
                        repo,
                        stages,
                        args.mode,
                        trace_logger,
                        stage_outputs_root,
                        start_stage=start_stage,
                    )

                print("All selected listings processed. Wai for confirm for 30s")
                await asyncio.sleep(30)
                
                await browser_context.close()
                await browser.close()
    except Exception:
        trace_logger.exception("Unhandled exception in real pipeline runner")
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


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()
