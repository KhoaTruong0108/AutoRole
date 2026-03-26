from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from autorole.config import AppConfig
from autorole.context import JobApplicationContext, JobListing
from autorole.db.repository import JobRepository
from autorole.gates.best_fit import BestFitGate
from autorole.integrations.credentials import CredentialStore
from autorole.pipeline import inject_loop_metadata_from_gate_reason
from autorole.stages.concluding import ConcludingStage
from autorole.stages.exploring import ExploringStage
from autorole.stages.form_intelligence import FormIntelligenceStage
from autorole.stages.form_submission import FormSubmissionStage
from autorole.stages.packaging import PackagingStage
from autorole.stages.scoring import CriterionDetail, CriterionScores, JDBreakdown, ScoringStage
from autorole.stages.session import SessionStage
from autorole.stages.tailoring import TailoringStage

try:
	from pipeline.types import Message
except Exception:
	class Message:  # pragma: no cover
		def __init__(self, run_id: str, payload: dict[str, Any], metadata: dict[str, Any] | None = None, attempt: int = 1) -> None:
			self.run_id = run_id
			self.payload = payload
			self.metadata = metadata or {}
			self.attempt = attempt


SAMPLE_LISTING = JobListing(
	job_url="https://acme.com/jobs/123",
	company_name="Acme Corp",
	job_id="123",
	job_title="Senior Engineer",
	platform="greenhouse",
	crawled_at=datetime.now(timezone.utc),
)


class MockScraper:
	async def search(self, _filters: Any) -> list[JobListing]:
		return [SAMPLE_LISTING]


class MockScorePage:
	def __init__(self) -> None:
		self.navigation_count = 0

	async def goto(self, _url: str, **_kwargs: Any) -> None:
		self.navigation_count += 1

	async def content(self) -> str:
		return "<html><body><h1>Sample JD</h1></body></html>"


class MockFormPage:
	def __init__(self, confirm_text: str = "application submitted") -> None:
		self.confirm_text = confirm_text
		self.fill_calls: list[tuple[str, str]] = []
		self.select_calls: list[tuple[str, str]] = []
		self.check_calls: list[str] = []
		self.file_calls: list[tuple[str, str]] = []

	class _Locator:
		def __init__(self, page: "MockFormPage", selector: str) -> None:
			self._page = page
			self._selector = selector
			self._value = ""

		@property
		def first(self) -> "MockFormPage._Locator":
			return self

		async def count(self) -> int:
			return 1

		async def wait_for(self, **_kwargs: Any) -> None:
			return None

		async def fill(self, value: str) -> None:
			self._value = value

		async def type(self, value: str, delay: int = 0) -> None:
			_ = delay
			self._value = value

		async def dispatch_event(self, _name: str) -> None:
			return None

		async def select_option(self, label: str) -> None:
			self._value = label

		async def click(self) -> None:
			return None

		async def check(self) -> None:
			return None

		async def set_input_files(self, path: str) -> None:
			self._page.file_calls.append((self._selector, path))

		async def all_text_contents(self) -> list[str]:
			return []

		async def inner_text(self) -> str:
			return "Application submitted"

	async def goto(self, _url: str, **_kwargs: Any) -> None:
		return None

	def locator(self, selector: str) -> "MockFormPage._Locator":
		return MockFormPage._Locator(self, selector)

	async def content(self) -> str:
		return self.confirm_text

	async def fill(self, selector: str, value: str) -> None:
		self.fill_calls.append((selector, value))

	async def select_option(self, selector: str, value: str) -> None:
		self.select_calls.append((selector, value))

	async def check(self, selector: str) -> None:
		self.check_calls.append(selector)

	async def uncheck(self, _selector: str) -> None:
		return None

	async def click(self, _selector: str) -> None:
		return None

	async def set_input_files(self, selector: str, path: str) -> None:
		self.file_calls.append((selector, path))

	async def wait_for_load_state(self, _state: str) -> None:
		return None


class MockRenderer:
	async def render(self, md_path: Path, pdf_path: Path) -> None:
		pdf_path.write_bytes(b"%PDF-1.7\n")


class MockLLMClient:
	def __init__(self, score_sequence: list[float]) -> None:
		self._scores = score_sequence
		self._score_idx = 0

	async def call(self, **kwargs: Any) -> Any:
		response_model = kwargs.get("response_model")
		if response_model is JDBreakdown:
			return JDBreakdown(
				qualifications=["python"],
				responsibilities=["backend systems"],
				required_skills=["python"],
				preferred_skills=["kubernetes"],
				culture_signals=["ownership"],
			)
		if response_model is CriterionScores:
			score = self._scores[min(self._score_idx, len(self._scores) - 1)]
			self._score_idx += 1
			scores = {
				"technical_skills": score,
				"experience_depth": score,
				"seniority_alignment": score,
				"domain_relevance": score,
				"culture_fit": score,
			}
			return CriterionScores(
				scores=scores,
				details={
					k: CriterionDetail(
						score=v,
						matched=[f"matched-{k}"],
						gaps=[] if v >= 0.7 else [f"gap-{k}"],
					)
					for k, v in scores.items()
				},
			)
		if response_model is None:
			return "# Tailored Resume\n\n- Added aligned content\n"
		raise AssertionError("Unexpected response_model")


class StubExtractor:
	async def extract(self, _section: Any, run_id: str, page_index: int) -> list[Any]:
		from autorole.integrations.form_controls.models import ExtractedField

		return [
			ExtractedField(
				id=f"{run_id}-email-{page_index}",
				run_id=run_id,
				page_index=page_index,
				page_label="Application form",
				field_type="text",
				selector="[name='email']",
				label="Email",
				required=True,
				options=[],
				prefilled_value="",
			),
		]


@pytest.mark.asyncio
async def test_full_pipeline_start_to_end(tmp_path: Path, monkeypatch: Any) -> None:
	base_dir = tmp_path / "autorole"
	resume_dir = base_dir / "resumes"
	resume_dir.mkdir(parents=True, exist_ok=True)
	master_resume = resume_dir / "master.md"
	master_resume.write_text("# Master Resume\n\n- Original\n", encoding="utf-8")

	config = AppConfig(
		base_dir=str(base_dir),
		resume_dir=str(resume_dir),
		db_path=str(base_dir / "pipeline.db"),
		master_resume=str(master_resume),
	)
	(base_dir / "user_profile.json").write_text("{}", encoding="utf-8")

	async with aiosqlite.connect(":memory:") as db:
		with open("src/autorole/db/migrations/001_domain.sql", encoding="utf-8") as f:
			await db.executescript(f.read())
		repo = JobRepository(db)

		llm = MockLLMClient(score_sequence=[0.72, 0.89])
		score_page = MockScorePage()
		form_page = MockFormPage(confirm_text="application submitted")

		exploring = ExploringStage(config, scrapers={"greenhouse": MockScraper()})
		scoring = ScoringStage(config, llm, score_page)
		tailoring = TailoringStage(config, llm)
		gate = BestFitGate(max_attempts=2)
		packaging = PackagingStage(config, MockRenderer())
		session = SessionStage(config, CredentialStore())
		form_intel = FormIntelligenceStage(
			config,
			llm,
			form_page,
			form_extractor=StubExtractor(),
			use_random_questionnaire_answers=True,
		)
		form_submit = FormSubmissionStage(config, form_page)
		concluding = ConcludingStage(config, repo)

		seed = Message(run_id="seed", payload={"search_config": {"platforms": ["greenhouse"]}})
		explore_result = await exploring.execute(seed)
		assert explore_result.success
		assert len(explore_result.output) == 1

		ctx = explore_result.output[0]
		await repo.upsert_listing(ctx.listing, ctx.run_id)

		async with db.execute("SELECT COUNT(*) FROM job_listings WHERE run_id = ?", (ctx.run_id,)) as cur:
			listing_count = await cur.fetchone()
		assert listing_count[0] == 1

		score1 = await scoring.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata={"source": "test"}, attempt=1))
		assert score1.success
		ctx = JobApplicationContext.model_validate(score1.output)
		await repo.upsert_score(ctx.run_id, ctx.score, attempt=1)
		assert score_page.navigation_count == 1

		tailor1 = await tailoring.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata={"source": "test"}, attempt=1))
		assert tailor1.success
		ctx = JobApplicationContext.model_validate(tailor1.output)
		await repo.upsert_tailored(ctx.run_id, ctx.tailored)

		gate1 = gate.evaluate(
			type("SR", (), {"output": ctx.model_dump()})(),
			Message(run_id=ctx.run_id, payload={}, metadata={}, attempt=1),
		)
		assert getattr(gate1.decision, "value", str(gate1.decision)) == "loop"
		metadata = inject_loop_metadata_from_gate_reason({}, gate1.reason)
		assert "last_score_before_tailoring" in metadata

		score2 = await scoring.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata=metadata, attempt=2))
		assert score2.success
		ctx = JobApplicationContext.model_validate(score2.output)
		await repo.upsert_score(ctx.run_id, ctx.score, attempt=2)
		# JD content should be reused from the first pass, so no extra navigation.
		assert score_page.navigation_count == 1

		tailor2 = await tailoring.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump(), metadata=metadata, attempt=2))
		assert tailor2.success
		ctx = JobApplicationContext.model_validate(tailor2.output)
		await repo.upsert_tailored(ctx.run_id, ctx.tailored)

		gate2 = gate.evaluate(
			type("SR", (), {"output": ctx.model_dump()})(),
			Message(run_id=ctx.run_id, payload={}, metadata=metadata, attempt=2),
		)
		assert getattr(gate2.decision, "value", str(gate2.decision)) == "pass"
		assert ctx.tailored.tailoring_degree == 0

		pack_result = await packaging.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))
		assert pack_result.success
		ctx = JobApplicationContext.model_validate(pack_result.output)

		session_result = await session.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))
		assert session_result.success
		ctx = JobApplicationContext.model_validate(session_result.output)
		await repo.upsert_session(ctx.run_id, ctx.session)

		intel_result = await form_intel.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))
		assert intel_result.success
		ctx = JobApplicationContext.model_validate(intel_result.output)

		submit_result = await form_submit.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))
		assert submit_result.success
		ctx = JobApplicationContext.model_validate(submit_result.output)

		done = await concluding.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))
		assert done.success

		async with db.execute("SELECT overall_score FROM score_reports WHERE run_id = ? AND attempt = 1", (ctx.run_id,)) as cur:
			row1 = await cur.fetchone()
		async with db.execute("SELECT overall_score FROM score_reports WHERE run_id = ? AND attempt = 2", (ctx.run_id,)) as cur:
			row2 = await cur.fetchone()
		assert row1 and row2
		assert row1[0] == pytest.approx(0.72)
		assert row2[0] == pytest.approx(0.89)

		async with db.execute(
			"SELECT COUNT(*) FROM score_reports WHERE run_id = ?",
			(ctx.run_id,),
		) as cur:
			score_count = await cur.fetchone()
		assert score_count[0] == 2

		async with db.execute(
			"SELECT tailoring_degree FROM tailored_resumes WHERE run_id = ? ORDER BY tailored_at DESC LIMIT 1",
			(ctx.run_id,),
		) as cur:
			tailor_row = await cur.fetchone()
		assert ctx.tailored.tailoring_degree >= 0
		assert tailor_row is not None
		assert tailor_row[0] >= 0

		async with db.execute(
			"SELECT authenticated FROM session_records WHERE run_id = ?",
			(ctx.run_id,),
		) as cur:
			session_row = await cur.fetchone()
		assert session_row is not None
		assert session_row[0] in (0, 1)

		async with db.execute(
			"SELECT submission_status FROM job_applications WHERE run_id = ?",
			(ctx.run_id,),
		) as cur:
			app_row = await cur.fetchone()
		assert app_row is not None
		assert app_row[0] == "submitted"
		assert form_page.file_calls, "Expected resume upload to be attempted"
