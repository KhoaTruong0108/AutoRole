from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiosqlite

from autorole.config import AppConfig
from autorole.context import JobApplicationContext
from autorole.db.repository import JobRepository
from autorole.gates.best_fit import BestFitGate
from autorole.gates.form_page import FormPageGate
from autorole.integrations.credentials import CredentialStore
from autorole.integrations.llm import AnthropicLLMClient, OllamaLLMClient, OpenAILLMClient
from autorole.integrations.renderer import PandocRenderer, WeasyPrintRenderer
from autorole.integrations.scrapers.indeed import IndeedScraper
from autorole.integrations.scrapers.linkedin import LinkedInScraper
from autorole.integrations.scrapers.url_posting import GenericJobPostingExtractor
from autorole.pipeline import inject_loop_metadata_from_gate_reason
from autorole.stage_base import AutoRoleStage, STAGE_ORDER, _emit_resume_hint
from autorole.stages.concluding import ConcludingExecutor, ConcludingStage
from autorole.stages.exploring import ExploringStage, ManualUrlExploringStage
from autorole.stages.form_intelligence import FormIntelligenceExecutor, FormIntelligenceStage
from autorole.stages.form_submission import FormSubmissionExecutor, FormSubmissionStage
from autorole.stages.packaging import PackagingExecutor, PackagingStage
from autorole.stages.scoring import ScoringExecutor, ScoringStage
from autorole.stages.session import SessionExecutor, SessionStage
from autorole.stages.tailoring import TailoringExecutor, TailoringStage


@dataclass
class Message:
	run_id: str
	payload: dict[str, Any]
	metadata: dict[str, Any]
	attempt: int = 1


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


async def _execute_stage(stage_name: str, stage: Any, msg: Message, logger: logging.Logger) -> Any | None:
	try:
		return await stage.execute(msg)
	except Exception:
		logger.exception("Unhandled exception in stage=%s run_id=%s", stage_name, msg.run_id)
		return None


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


class JobApplicationPipeline:
	STAGE_ORDER = STAGE_ORDER

	def __init__(self, config: AppConfig, run_config: RunConfig) -> None:
		self._config = config
		self._rc = run_config
		self._repo: JobRepository | None = None

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
		trace_logger.info("Starting real pipeline run mode=%s", rc.mode)
		stage_outputs_root.mkdir(parents=True, exist_ok=True)
		trace_logger.info("STAGE_OUTPUTS_ROOT path=%s", stage_outputs_root)
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
				self._repo = repo

				llm_client = make_llm_client(config)
				renderer = make_renderer(config)

				async with async_playwright() as playwright:
					browser = await playwright.chromium.launch(headless=rc.headless)
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
						platform_hint = rc.job_platform.strip() or None
						exploring = ManualUrlExploringStage(config, extractor=extractor, platform_hint=platform_hint)
						seed_payload: dict[str, Any] = {"job_url": rc.job_url.strip()}
						print("Starting exploration in manual URL mode...")
					else:
						exploring = ExploringStage(config, scrapers=scrapers)
						seed_payload = {"search_config": search_config}
						print("Starting exploration with real scrapers...")

					executors = self._build_executors(
						repo=repo,
						logger=trace_logger,
						stage_outputs_root=stage_outputs_root,
						llm_client=llm_client,
						renderer=renderer,
						score_page=score_page,
						form_page=form_page,
					)
					gate = BestFitGate(max_attempts=config.tailoring.max_attempts)

					start_stage = "exploring"
					if is_resume_mode:
						resume_run_id = rc.resume_run_id.strip()
						checkpoint = await repo.get_checkpoint(resume_run_id)
						if checkpoint is None:
							print(f"No checkpoint found for run_id={resume_run_id}")
							await browser_context.close()
							await browser.close()
							return 1

						last_success_stage, checkpoint_ctx = checkpoint
						resume_ctx = JobApplicationContext.model_validate(checkpoint_ctx)
						if rc.from_stage:
							start_stage = rc.from_stage
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
							_emit_resume_hint(trace_logger, "seed", rc.mode, "exploring")
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
							_emit_resume_hint(trace_logger, "seed", rc.mode, "exploring")
							await browser_context.close()
							await browser.close()
							return 1

						contexts: list[JobApplicationContext] = explore_result.output
						selected = contexts[: max(1, rc.max_listings)]
						print(f"Exploring found {len(contexts)} listing(s); processing {len(selected)}")

					for listing_ctx in selected:
						succeeded = await self._run_listing(
							ctx=listing_ctx,
							executors=executors,
							gate=gate,
							logger=trace_logger,
							artifacts_root=stage_outputs_root,
							start_stage=start_stage,
						)
						if not succeeded:
							print(f"[block] run_id={listing_ctx.run_id} halted due to stage failure")
							await browser_context.close()
							await browser.close()
							return 1

					print("All selected listings processed. Wait for confirming for 30s")
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

	async def _run_listing(
		self,
		ctx: JobApplicationContext,
		executors: dict[str, AutoRoleStage],
		gate: BestFitGate,
		logger: logging.Logger,
		artifacts_root: Path,
		start_stage: str,
	) -> bool:
		if self._repo is None:
			raise RuntimeError("JobRepository is not initialized")

		repo = self._repo
		mode = self._rc.mode
		print(f"\n=== RUN {ctx.run_id} ===")
		logger.info("Starting listing run_id=%s mode=%s start_stage=%s", ctx.run_id, mode, start_stage)

		run_artifacts_dir = artifacts_root / ctx.run_id
		run_artifacts_dir.mkdir(parents=True, exist_ok=True)
		stage_index_path = run_artifacts_dir / "stage_outputs.md"
		if stage_index_path.exists() and start_stage != "exploring":
			with stage_index_path.open("a", encoding="utf-8") as handle:
				handle.write(f"\n## Resumed from {start_stage} at {datetime.now(timezone.utc).isoformat()}\n")
		else:
			stage_index_path.write_text(f"# Stage Outputs for {ctx.run_id}\n\n", encoding="utf-8")
		logger.info("RUN_ARTIFACT_INDEX run_id=%s path=%s", ctx.run_id, stage_index_path)

		listing_json = ctx.listing.model_dump(mode="json") if ctx.listing is not None else {}
		listing_path = run_artifacts_dir / "exploring" / "listing.json"
		listing_path.parent.mkdir(parents=True, exist_ok=True)
		listing_path.write_text(
			json.dumps(listing_json, indent=2, ensure_ascii=False) + "\n",
			encoding="utf-8",
		)
		with stage_index_path.open("a", encoding="utf-8") as handle:
			handle.write("- exploring: exploring/listing.json\n")
		logger.info("STAGE_ARTIFACT stage=exploring path=%s", listing_path)

		if executors["scoring"].should_run(start_stage):
			if ctx.listing is not None:
				await repo.upsert_listing(ctx.listing, ctx.run_id)
			await repo.upsert_checkpoint(ctx.run_id, "exploring", ctx.model_dump(mode="json"))
			print("[ok] exploring -> listing saved")
		else:
			print(f"[resume] skipping exploring (start stage: {start_stage})")

		metadata: dict[str, Any] = {}
		attempt = 1
		if executors["scoring"].should_run(start_stage) or executors["tailoring"].should_run(start_stage):
			while True:
				ctx = await executors["scoring"].run(ctx, attempt=attempt, metadata=metadata)
				if ctx is None:
					return False
				ctx = await executors["tailoring"].run(ctx, attempt=attempt, metadata=metadata)
				if ctx is None:
					return False

				gate_result = gate.evaluate(
					SimpleNamespace(output=ctx.model_dump()),
					Message(run_id=ctx.run_id, payload={}, metadata=metadata, attempt=attempt),
				)
				decision = getattr(gate_result.decision, "value", str(gate_result.decision))
				if decision == "loop":
					metadata = inject_loop_metadata_from_gate_reason(metadata, gate_result.reason)
					attempt += 1
					logger.info("best_fit loop run_id=%s reason=%s", ctx.run_id, gate_result.reason)
					print(f"[loop] best_fit -> {gate_result.reason}")
					continue
				if decision == "block":
					logger.warning("best_fit block run_id=%s reason=%s", ctx.run_id, gate_result.reason)
					print(f"[block] best_fit -> {gate_result.reason}")
					return False

				print("[ok] best_fit -> pass")
				break
		else:
			print(f"[resume] skipping scoring/tailoring/best_fit (start stage: {start_stage})")

		if mode == "observe":
			print("[stop] observe mode enabled; skipping packaging, session, and submission stages")
			return True

		for stage_name in ["packaging", "session"]:
			ex = executors[stage_name]
			if not ex.should_run(start_stage):
				print(f"[resume] skipping {stage_name} (start stage: {start_stage})")
				continue
			ctx = await ex.run(ctx)
			if ctx is None:
				return False

		# --- Form filling loop ---
		if executors["form_intelligence"].should_run(start_stage):
			while True:
				ctx = await executors["form_intelligence"].run(ctx)
				if ctx is None:
					return False

				ctx = await executors["form_submission"].run(ctx)
				if ctx is None:
					return False

				if self._rc.mode == "apply-dryrun":
					print("[stop] apply-dryrun mode; stopping after first form page fill")
					return True

				gate_result = executors["form_page_gate"].evaluate(
					SimpleNamespace(output=ctx.model_dump()),
					Message(run_id=ctx.run_id, payload={}, metadata={}, attempt=1),
				)
				decision = getattr(gate_result.decision, "value", str(gate_result.decision))

				if decision == "loop":
					print(f"[loop] form_page_gate -> {gate_result.reason}")
					continue
				if decision == "block":
					print(f"[block] form_page_gate -> {gate_result.reason}")
					_emit_resume_hint(logger, ctx.run_id, self._rc.mode, "form_intelligence")
					return False

				print("[ok] form_page_gate -> submitted")
				break
		elif executors["form_submission"].should_run(start_stage):
			if ctx.form_intelligence is None or ctx.form_session is None:
				print("[compat] hydrating form context via form_intelligence before form_submission resume")
				ctx = await executors["form_intelligence"].run(ctx)
				if ctx is None:
					return False
			ctx = await executors["form_submission"].run(ctx)
			if ctx is None:
				return False
		else:
			print(f"[resume] skipping form loop (start stage: {start_stage})")

		if mode == "apply-dryrun":
			print("[stop] apply-dryrun mode enabled; completed flow with submit click skipped")
			return True

		ex = executors["concluding"]
		if ex.should_run(start_stage):
			ctx = await ex.run(ctx)
			if ctx is None:
				return False
		else:
			print(f"[resume] skipping concluding (start stage: {start_stage})")

		print(
			f"[done] run_id={ctx.run_id} score={ctx.score.overall_score:.3f} "
			f"tailoring_degree={ctx.tailored.tailoring_degree}"
		)
		return True

	def _build_executors(
		self,
		repo: JobRepository,
		logger: logging.Logger,
		stage_outputs_root: Path,
		llm_client: Any,
		renderer: Any,
		score_page: Any,
		form_page: Any,
	) -> dict[str, AutoRoleStage]:
		cfg = self._config
		mode = self._rc.mode
		args = (repo, logger, stage_outputs_root, mode, cfg)
		return {
			"scoring": ScoringExecutor(ScoringStage(cfg, llm_client, score_page), *args),
			"tailoring": TailoringExecutor(TailoringStage(cfg, llm_client), *args),
			"packaging": PackagingExecutor(PackagingStage(cfg, renderer), *args),
			"session": SessionExecutor(SessionStage(cfg, CredentialStore()), *args),
			"form_intelligence": FormIntelligenceExecutor(
				FormIntelligenceStage(
					cfg,
					llm_client,
					form_page,
					use_random_questionnaire_answers=mode in {"observe"},
				),
				*args,
			),
			"form_submission": FormSubmissionExecutor(FormSubmissionStage(cfg, form_page), *args),
			"form_page_gate": FormPageGate(),
			"concluding": ConcludingExecutor(ConcludingStage(cfg, repo), *args),
		}


__all__ = ["JobApplicationPipeline", "RunConfig"]
