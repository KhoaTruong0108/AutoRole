from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from autorole.config import AppConfig, RetentionConfig
from autorole.context import (
	ApplicationResult,
	JobApplicationContext,
	PackagedResume,
	ScoreReport,
	SessionResult,
	TailoredResume,
)
from autorole.db.repository import JobRepository
from autorole.stages.concluding import ConcludingStage
from tests.conftest import SAMPLE_LISTING

try:
	from pipeline.types import Message
except Exception:
	class Message:  # pragma: no cover - fallback when pipeline package is unavailable
		def __init__(self, run_id: str, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
			self.run_id = run_id
			self.payload = payload
			self.metadata = metadata or {}


@pytest.fixture
async def repo_db() -> tuple[aiosqlite.Connection, JobRepository]:
	db = await aiosqlite.connect(":memory:")
	with open("src/autorole/db/migrations/001_domain.sql", encoding="utf-8") as handle:
		await db.executescript(handle.read())
	repo = JobRepository(db)
	yield db, repo
	await db.close()


def _full_context(run_id: str = "acme_123") -> JobApplicationContext:
	now = datetime.now(timezone.utc)
	return JobApplicationContext(
		run_id=run_id,
		listing=SAMPLE_LISTING.model_copy(update={"job_id": "123", "company_name": "Acme Corp"}),
		score=ScoreReport(
			resume_id="master",
			jd_html="<html></html>",
			jd_breakdown={},
			overall_score=0.83,
			criteria_scores={},
			matched=[],
			mismatched=[],
			scored_at=now,
		),
		tailored=TailoredResume(
			resume_id="res-1",
			parent_resume_id="master",
			tailoring_degree=1,
			file_path="/tmp/res-1.md",
			diff_summary="{}",
			tailored_at=now,
		),
		packaged=PackagedResume(
			resume_id="res-1",
			pdf_path="/tmp/res-1.pdf",
			packaged_at=now,
		),
		session=SessionResult(
			platform="linkedin",
			authenticated=True,
			session_note="ok",
			established_at=now,
		),
		applied=ApplicationResult(
			resume_id="res-1",
			questionnaire=[],
			form_json={},
			submission_status="submitted",
			submission_confirmed=True,
			applied_at=now,
		),
	)


async def test_concluding_writes_application_record(repo_db: tuple[aiosqlite.Connection, JobRepository], tmp_path: Path) -> None:
	db, repo = repo_db
	ctx = _full_context()
	await repo.upsert_listing(ctx.listing, ctx.run_id)

	config = AppConfig(
		base_dir=str(tmp_path),
		resume_dir=str(tmp_path / "resumes"),
		db_path=str(tmp_path / "pipeline.db"),
		master_resume=str(tmp_path / "resumes" / "master.md"),
	)
	stage = ConcludingStage(config, repo)

	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))
	assert result.success

	async with db.execute(
		"SELECT run_id, resume_id, pdf_path, submission_status, submission_confirmed, overall_score, tailoring_degree "
		"FROM job_applications WHERE run_id = ?",
		(ctx.run_id,),
	) as cur:
		row = await cur.fetchone()

	assert row is not None
	assert row[0] == ctx.run_id
	assert row[1] == ctx.applied.resume_id
	assert row[2] == ctx.packaged.pdf_path
	assert row[3] == "submitted"
	assert row[4] == 1
	assert row[5] == ctx.score.overall_score
	assert row[6] == ctx.tailored.tailoring_degree


async def test_concluding_fails_when_any_context_field_is_none(
	repo_db: tuple[aiosqlite.Connection, JobRepository], tmp_path: Path
) -> None:
	_db, repo = repo_db
	ctx = JobApplicationContext(run_id="acme_123", listing=SAMPLE_LISTING)

	config = AppConfig(
		base_dir=str(tmp_path),
		resume_dir=str(tmp_path / "resumes"),
		db_path=str(tmp_path / "pipeline.db"),
		master_resume=str(tmp_path / "resumes" / "master.md"),
	)
	stage = ConcludingStage(config, repo)

	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))
	assert not result.success
	assert result.error_type == "PreconditionError"


async def test_concluding_prunes_old_files_when_auto_prune_enabled(
	repo_db: tuple[aiosqlite.Connection, JobRepository], tmp_path: Path
) -> None:
	db, repo = repo_db
	ctx = _full_context()
	await repo.upsert_listing(ctx.listing, ctx.run_id)

	old_md = tmp_path / "old_resume.md"
	old_pdf = tmp_path / "old_resume.pdf"
	old_md.write_text("old", encoding="utf-8")
	old_pdf.write_text("old", encoding="utf-8")

	old_ts = (datetime.now(timezone.utc) - timedelta(days=500)).isoformat()
	await db.execute(
		"INSERT INTO tailored_resumes (resume_id, parent_resume_id, run_id, tailoring_degree, file_path, diff_summary, tailored_at) "
		"VALUES (?, ?, ?, ?, ?, ?, ?)",
		("old-res", "master", ctx.run_id, 1, str(old_md), "{}", old_ts),
	)
	await db.execute(
		"INSERT INTO job_applications (run_id, resume_id, pdf_path, submission_status, submission_confirmed, overall_score, tailoring_degree, applied_at) "
		"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
		("old-run", "old-res", str(old_pdf), "submitted", 1, 0.8, 1, old_ts),
	)
	await db.commit()

	config = AppConfig(
		base_dir=str(tmp_path),
		resume_dir=str(tmp_path / "resumes"),
		db_path=str(tmp_path / "pipeline.db"),
		master_resume=str(tmp_path / "resumes" / "master.md"),
		retention=RetentionConfig(max_age_days=365, auto_prune=True),
	)
	stage = ConcludingStage(config, repo)

	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))
	assert result.success
	assert not old_md.exists()
	assert not old_pdf.exists()


async def test_concluding_skips_prune_when_disabled(
	repo_db: tuple[aiosqlite.Connection, JobRepository], tmp_path: Path
) -> None:
	_db, repo = repo_db
	ctx = _full_context()

	keep_file = tmp_path / "keep.md"
	keep_file.write_text("keep", encoding="utf-8")

	config = AppConfig(
		base_dir=str(tmp_path),
		resume_dir=str(tmp_path / "resumes"),
		db_path=str(tmp_path / "pipeline.db"),
		master_resume=str(tmp_path / "resumes" / "master.md"),
		retention=RetentionConfig(max_age_days=365, auto_prune=False),
	)
	stage = ConcludingStage(config, repo)

	# No listing upsert: we expect a DB failure if stage reaches write, so insert listing first.
	await repo.upsert_listing(ctx.listing, ctx.run_id)
	result = await stage.execute(Message(run_id=ctx.run_id, payload=ctx.model_dump()))
	assert result.success
	assert keep_file.exists()


async def test_repository_upsert_is_idempotent(repo_db: tuple[aiosqlite.Connection, JobRepository]) -> None:
	db, repo = repo_db
	listing = SAMPLE_LISTING.model_copy(update={"job_id": "999", "company_name": "Idem Corp"})
	await repo.upsert_listing(listing, "idem_999")
	await repo.upsert_listing(listing, "idem_999")

	async with db.execute("SELECT COUNT(*) FROM job_listings WHERE run_id = ?", ("idem_999",)) as cur:
		row = await cur.fetchone()
	assert row[0] == 1
