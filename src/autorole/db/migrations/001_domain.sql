CREATE TABLE IF NOT EXISTS resumes (
	resume_id    TEXT    PRIMARY KEY,
	file_path    TEXT    NOT NULL,
	is_master    INTEGER NOT NULL DEFAULT 0,
	created_at   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS job_listings (
	run_id        TEXT PRIMARY KEY,
	job_url       TEXT NOT NULL,
	company_name  TEXT NOT NULL,
	job_id        TEXT NOT NULL,
	job_title     TEXT NOT NULL,
	platform      TEXT NOT NULL,
	crawled_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_listings_company ON job_listings(company_name);
CREATE INDEX IF NOT EXISTS idx_listings_platform ON job_listings(platform);

CREATE TABLE IF NOT EXISTS score_reports (
	id              INTEGER PRIMARY KEY AUTOINCREMENT,
	run_id          TEXT    NOT NULL REFERENCES job_listings(run_id),
	resume_id       TEXT    NOT NULL,
	attempt         INTEGER NOT NULL DEFAULT 1,
	jd_html         TEXT,
	jd_breakdown    TEXT,
	overall_score   REAL    NOT NULL,
	criteria_scores TEXT,
	matched         TEXT,
	mismatched      TEXT,
	scored_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scores_run_id ON score_reports(run_id);

CREATE TABLE IF NOT EXISTS tailored_resumes (
	resume_id        TEXT PRIMARY KEY,
	parent_resume_id TEXT NOT NULL,
	run_id           TEXT NOT NULL REFERENCES job_listings(run_id),
	tailoring_degree INTEGER NOT NULL,
	file_path        TEXT NOT NULL,
	diff_summary     TEXT,
	tailored_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS session_records (
	run_id         TEXT PRIMARY KEY REFERENCES job_listings(run_id),
	platform       TEXT NOT NULL,
	authenticated  INTEGER NOT NULL DEFAULT 0,
	session_note   TEXT,
	established_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_applications (
	run_id               TEXT PRIMARY KEY REFERENCES job_listings(run_id),
	resume_id            TEXT,
	pdf_path             TEXT,
	submission_status    TEXT,
	submission_confirmed INTEGER,
	overall_score        REAL,
	tailoring_degree     INTEGER,
	applied_at           TEXT,
	created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_applications_status ON job_applications(submission_status);
CREATE INDEX IF NOT EXISTS idx_applications_date ON job_applications(applied_at DESC);

CREATE TABLE IF NOT EXISTS pipeline_checkpoints (
	run_id             TEXT PRIMARY KEY REFERENCES job_listings(run_id),
	last_success_stage TEXT NOT NULL,
	context_json       TEXT NOT NULL,
	updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
