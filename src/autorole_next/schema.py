DOMAIN_TABLES = [
    "listings",
    "score_reports",
    "tailored_resumes",
    "sessions",
    "applications",
]


DOMAIN_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS queue_messages (
    id TEXT PRIMARY KEY,
    queue_name TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    visible_at REAL NOT NULL,
    locked_until REAL,
    delivery_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_queue_claim
ON queue_messages (queue_name, visible_at, locked_until);

CREATE TABLE IF NOT EXISTS listings (
    listing_key TEXT PRIMARY KEY,
    correlation_id TEXT NOT NULL UNIQUE,
    source_name TEXT NOT NULL,
    source_metadata TEXT NOT NULL DEFAULT '{}',
    job_url TEXT NOT NULL,
    apply_url TEXT,
    company_name TEXT NOT NULL,
    job_title TEXT NOT NULL,
    external_job_id TEXT,
    platform TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'seeded',
    discovered_at TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_listings_platform ON listings(platform);
CREATE INDEX IF NOT EXISTS idx_listings_company_name ON listings(company_name);

CREATE TABLE IF NOT EXISTS score_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    overall_score REAL NOT NULL,
    criteria_json TEXT,
    matched_json TEXT,
    mismatched_json TEXT,
    jd_summary TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_score_reports_correlation_attempt
ON score_reports(correlation_id, attempt);

CREATE TABLE IF NOT EXISTS tailored_resumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    resume_path TEXT NOT NULL,
    diff_summary TEXT,
    tailoring_degree INTEGER,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tailored_resumes_correlation_attempt
ON tailored_resumes(correlation_id, attempt);

CREATE TABLE IF NOT EXISTS sessions (
    correlation_id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    authenticated INTEGER NOT NULL,
    session_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    correlation_id TEXT PRIMARY KEY,
    status TEXT,
    confirmed INTEGER,
    applied_at TEXT,
    resume_path TEXT,
    pdf_path TEXT,
    final_score REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
"""
