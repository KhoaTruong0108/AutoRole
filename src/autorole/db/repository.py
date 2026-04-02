from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import aiosqlite
import orjson

from autorole.application_status import is_terminal_application_status
from autorole.context import (
	ApplicationResult,
	JobListing,
	PackagedResume,
	ScoreReport,
	SessionResult,
	TailoredResume,
)
from autorole.integrations.discovery.normalization import canonical_listing_key, normalize_listing


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
		terminal_status = applied.submission_status if applied else None
		canonical_key = ""
		if listing is not None and is_terminal_application_status(terminal_status):
			normalized_listing = normalize_listing(listing)
			canonical_key = canonical_listing_key(normalized_listing)
			existing = await self.get_listing_identity(canonical_key)
			if existing is not None:
				existing_run_id = str(existing.get("run_id") or "").strip()
				if existing_run_id and existing_run_id != run_id:
					existing_status = await self.get_application_status(existing_run_id)
					if is_terminal_application_status(existing_status):
						return

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
		if canonical_key and listing is not None:
			await self.upsert_listing_identity(canonical_key, listing, run_id=run_id)
		await self._db.commit()

	async def get_application_status(self, run_id: str) -> str | None:
		async with self._db.execute(
			"SELECT submission_status FROM job_applications WHERE run_id = ?",
			(run_id,),
		) as cursor:
			row = await cursor.fetchone()
		if row is None:
			return None
		return str(row[0]) if row[0] is not None else None

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

	async def upsert_checkpoint(self, run_id: str, last_success_stage: str, context: dict[str, Any]) -> None:
		await self._db.execute(
			"""
			INSERT INTO pipeline_checkpoints (run_id, last_success_stage, context_json, updated_at)
			VALUES (?, ?, ?, ?)
			ON CONFLICT(run_id) DO UPDATE SET
				last_success_stage = excluded.last_success_stage,
				context_json = excluded.context_json,
				updated_at = excluded.updated_at
			""",
			(
				run_id,
				last_success_stage,
				orjson.dumps(context).decode("utf-8"),
				datetime.now(timezone.utc).isoformat(),
			),
		)
		await self._db.commit()

	async def get_checkpoint(self, run_id: str) -> tuple[str, dict[str, Any]] | None:
		async with self._db.execute(
			"""
			SELECT last_success_stage, context_json
			FROM pipeline_checkpoints
			WHERE run_id = ?
			""",
			(run_id,),
		) as cursor:
			row = await cursor.fetchone()
		if row is None:
			return None

		stage = str(row[0])
		context = orjson.loads(row[1])
		return stage, context

	async def claim_listing_identity(
		self,
		canonical_key: str,
		listing: JobListing,
		run_id: str | None = None,
	) -> bool:
		cursor = await self._db.execute(
			"""
			INSERT INTO listing_identities (
				canonical_key, run_id, job_url, apply_url, company_name, job_id,
				job_title, platform, crawled_at, created_at, updated_at
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(canonical_key) DO NOTHING
			""",
			(
				canonical_key,
				run_id,
				listing.job_url,
				listing.apply_url,
				listing.company_name,
				listing.job_id,
				listing.job_title,
				listing.platform,
				listing.crawled_at.isoformat(),
				datetime.now(timezone.utc).isoformat(),
				datetime.now(timezone.utc).isoformat(),
			),
		)
		await self._db.commit()
		return cursor.rowcount > 0

	async def upsert_listing_identity(
		self,
		canonical_key: str,
		listing: JobListing,
		run_id: str | None = None,
	) -> None:
		now = datetime.now(timezone.utc).isoformat()
		await self._db.execute(
			"""
			INSERT INTO listing_identities (
				canonical_key, run_id, job_url, apply_url, company_name, job_id,
				job_title, platform, crawled_at, created_at, updated_at
			) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
			ON CONFLICT(canonical_key) DO UPDATE SET
				run_id = excluded.run_id,
				job_url = excluded.job_url,
				apply_url = excluded.apply_url,
				company_name = excluded.company_name,
				job_id = excluded.job_id,
				job_title = excluded.job_title,
				platform = excluded.platform,
				crawled_at = excluded.crawled_at,
				updated_at = excluded.updated_at
			""",
			(
				canonical_key,
				run_id,
				listing.job_url,
				listing.apply_url,
				listing.company_name,
				listing.job_id,
				listing.job_title,
				listing.platform,
				listing.crawled_at.isoformat(),
				now,
				now,
			),
		)

	async def get_listing_identity(self, canonical_key: str) -> dict[str, Any] | None:
		async with self._db.execute(
			"""
			SELECT canonical_key, run_id, job_url, apply_url, company_name, job_id,
			       job_title, platform, crawled_at, created_at, updated_at
			FROM listing_identities
			WHERE canonical_key = ?
			""",
			(canonical_key,),
		) as cursor:
			row = await cursor.fetchone()
		if row is None:
			return None
		return {
			"canonical_key": row[0],
			"run_id": row[1],
			"job_url": row[2],
			"apply_url": row[3],
			"company_name": row[4],
			"job_id": row[5],
			"job_title": row[6],
			"platform": row[7],
			"crawled_at": row[8],
			"created_at": row[9],
			"updated_at": row[10],
		}
