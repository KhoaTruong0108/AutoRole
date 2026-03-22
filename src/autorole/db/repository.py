from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite
import orjson

from autorole.context import (
	ApplicationResult,
	JobListing,
	PackagedResume,
	ScoreReport,
	SessionResult,
	TailoredResume,
)


class JobRepository:
	def __init__(self, db: aiosqlite.Connection) -> None:
		self._db = db

	async def upsert_listing(self, listing: JobListing, run_id: str) -> None:
		await self._db.execute(
			"""
			INSERT INTO job_listings (
				run_id, job_url, company_name, job_id, job_title, platform, crawled_at
			) VALUES (?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(run_id) DO UPDATE SET
				job_url = excluded.job_url,
				company_name = excluded.company_name,
				job_id = excluded.job_id,
				job_title = excluded.job_title,
				platform = excluded.platform,
				crawled_at = excluded.crawled_at
			""",
			(
				run_id,
				listing.job_url,
				listing.company_name,
				listing.job_id,
				listing.job_title,
				listing.platform,
				listing.crawled_at.isoformat(),
			),
		)
		await self._db.commit()

	async def upsert_score(self, run_id: str, score: ScoreReport, attempt: int) -> None:
		await self._db.execute(
			"""
			INSERT INTO score_reports (
				run_id, resume_id, attempt, jd_html, jd_breakdown,
				overall_score, criteria_scores, matched, mismatched, scored_at
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			""",
			(
				run_id,
				score.resume_id,
				attempt,
				score.jd_html,
				orjson.dumps(score.jd_breakdown).decode("utf-8"),
				score.overall_score,
				orjson.dumps(score.criteria_scores).decode("utf-8"),
				orjson.dumps(score.matched).decode("utf-8"),
				orjson.dumps(score.mismatched).decode("utf-8"),
				score.scored_at.isoformat(),
			),
		)
		await self._db.commit()

	async def upsert_tailored(self, run_id: str, resume: TailoredResume) -> None:
		await self._db.execute(
			"""
			INSERT INTO tailored_resumes (
				resume_id, parent_resume_id, run_id, tailoring_degree,
				file_path, diff_summary, tailored_at
			) VALUES (?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(resume_id) DO UPDATE SET
				parent_resume_id = excluded.parent_resume_id,
				run_id = excluded.run_id,
				tailoring_degree = excluded.tailoring_degree,
				file_path = excluded.file_path,
				diff_summary = excluded.diff_summary,
				tailored_at = excluded.tailored_at
			""",
			(
				resume.resume_id,
				resume.parent_resume_id,
				run_id,
				resume.tailoring_degree,
				resume.file_path,
				resume.diff_summary,
				resume.tailored_at.isoformat(),
			),
		)
		await self._db.commit()

	async def upsert_session(self, run_id: str, session: SessionResult) -> None:
		await self._db.execute(
			"""
			INSERT INTO session_records (
				run_id, platform, authenticated, session_note, established_at
			) VALUES (?, ?, ?, ?, ?)
			ON CONFLICT(run_id) DO UPDATE SET
				platform = excluded.platform,
				authenticated = excluded.authenticated,
				session_note = excluded.session_note,
				established_at = excluded.established_at
			""",
			(
				run_id,
				session.platform,
				int(session.authenticated),
				session.session_note,
				session.established_at.isoformat(),
			),
		)
		await self._db.commit()

	async def upsert_application(
		self,
		run_id: str,
		listing: JobListing | None,
		score: ScoreReport | None,
		tailored: TailoredResume | None,
		packaged: PackagedResume | None,
		applied: ApplicationResult | None,
	) -> None:
		await self._db.execute(
			"""
			INSERT INTO job_applications (
				run_id, resume_id, pdf_path, submission_status, submission_confirmed,
				overall_score, tailoring_degree, applied_at
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(run_id) DO UPDATE SET
				resume_id = excluded.resume_id,
				pdf_path = excluded.pdf_path,
				submission_status = excluded.submission_status,
				submission_confirmed = excluded.submission_confirmed,
				overall_score = excluded.overall_score,
				tailoring_degree = excluded.tailoring_degree,
				applied_at = excluded.applied_at
			""",
			(
				run_id,
				(applied.resume_id if applied else (packaged.resume_id if packaged else None)),
				(packaged.pdf_path if packaged else None),
				(applied.submission_status if applied else None),
				(int(applied.submission_confirmed) if applied else None),
				(score.overall_score if score else None),
				(tailored.tailoring_degree if tailored else None),
				(applied.applied_at.isoformat() if applied else None),
			),
		)
		await self._db.commit()

	async def get_pruneable_files(self, max_age_days: int) -> list[str]:
		cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
		async with self._db.execute(
			"""
			SELECT file_path FROM tailored_resumes WHERE tailored_at < ?
			UNION
			SELECT pdf_path FROM job_applications WHERE applied_at < ?
			""",
			(cutoff, cutoff),
		) as cursor:
			rows = await cursor.fetchall()
		return [row[0] for row in rows if row[0]]
