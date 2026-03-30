-- Drop REFERENCES(job_listings.run_id) from domain tables using SQLite recreate pattern.

CREATE TABLE IF NOT EXISTS listing_identities (
    canonical_key TEXT PRIMARY KEY,
    run_id        TEXT,
    job_url       TEXT NOT NULL,
    apply_url     TEXT,
    company_name  TEXT NOT NULL,
    job_id        TEXT NOT NULL,
    job_title     TEXT NOT NULL,
    platform      TEXT NOT NULL,
    crawled_at    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listing_identities_run_id ON listing_identities(run_id);

DROP TABLE IF EXISTS score_reports_new;
CREATE TABLE score_reports_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT    NOT NULL,
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
INSERT INTO score_reports_new SELECT * FROM score_reports;
DROP TABLE score_reports;
ALTER TABLE score_reports_new RENAME TO score_reports;
CREATE INDEX IF NOT EXISTS idx_scores_run_id ON score_reports(run_id);

DROP TABLE IF EXISTS tailored_resumes_new;
CREATE TABLE tailored_resumes_new (
    resume_id        TEXT PRIMARY KEY,
    parent_resume_id TEXT NOT NULL,
    run_id           TEXT NOT NULL,
    tailoring_degree INTEGER NOT NULL,
    file_path        TEXT NOT NULL,
    diff_summary     TEXT,
    tailored_at      TEXT    NOT NULL
);
INSERT INTO tailored_resumes_new SELECT * FROM tailored_resumes;
DROP TABLE tailored_resumes;
ALTER TABLE tailored_resumes_new RENAME TO tailored_resumes;

DROP TABLE IF EXISTS session_records_new;
CREATE TABLE session_records_new (
    run_id         TEXT PRIMARY KEY,
    platform       TEXT NOT NULL,
    authenticated  INTEGER NOT NULL DEFAULT 0,
    session_note   TEXT,
    established_at TEXT NOT NULL
);
INSERT INTO session_records_new SELECT * FROM session_records;
DROP TABLE session_records;
ALTER TABLE session_records_new RENAME TO session_records;

DROP TABLE IF EXISTS job_applications_new;
CREATE TABLE job_applications_new (
    run_id               TEXT PRIMARY KEY,
    resume_id            TEXT,
    pdf_path             TEXT,
    submission_status    TEXT,
    submission_confirmed INTEGER,
    overall_score        REAL,
    tailoring_degree     INTEGER,
    applied_at           TEXT,
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO job_applications_new SELECT * FROM job_applications;
DROP TABLE job_applications;
ALTER TABLE job_applications_new RENAME TO job_applications;
CREATE INDEX IF NOT EXISTS idx_applications_status ON job_applications(submission_status);
CREATE INDEX IF NOT EXISTS idx_applications_date ON job_applications(applied_at DESC);

DROP TABLE IF EXISTS pipeline_checkpoints_new;
CREATE TABLE pipeline_checkpoints_new (
    run_id             TEXT PRIMARY KEY,
    last_success_stage TEXT NOT NULL,
    context_json       TEXT NOT NULL,
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO pipeline_checkpoints_new SELECT * FROM pipeline_checkpoints;
DROP TABLE pipeline_checkpoints;
ALTER TABLE pipeline_checkpoints_new RENAME TO pipeline_checkpoints;

CREATE TABLE IF NOT EXISTS queue_messages (
    message_id          TEXT    PRIMARY KEY,
    queue_name          TEXT    NOT NULL,
    run_id              TEXT    NOT NULL,
    stage               TEXT    NOT NULL,
    payload             TEXT    NOT NULL,
    attempt             INTEGER NOT NULL DEFAULT 1,
    reply_queue         TEXT    NOT NULL,
    dead_letter_queue   TEXT    NOT NULL,
    metadata            TEXT    NOT NULL,
    status              TEXT    NOT NULL DEFAULT 'pending',
    enqueued_at         TEXT    NOT NULL,
    visible_after       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_queue_pull
    ON queue_messages(queue_name, status, visible_after);
