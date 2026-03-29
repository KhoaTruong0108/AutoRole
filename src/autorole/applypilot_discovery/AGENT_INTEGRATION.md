# Integrating `applypilot_discovery` into a New Package

This file is an integration guide for an AI agent. The entire `applypilot_discovery/` folder
can be copied verbatim into another Python package. The only work required is:

1. Wiring up the **runtime adapter** (3 interfaces)
2. Copying the **config YAML files**
3. Adding **dependencies**

---

## What this package does

Three independent job discovery scrapers, all writing to a shared SQLite `jobs` table:

| Module | What it does | External tools |
|--------|-------------|----------------|
| `jobspy.py` | Scrapes Indeed, LinkedIn, Glassdoor, ZipRecruiter via `python-jobspy` | jobspy, pandas |
| `workday.py` | Hits the undocumented Workday CXS JSON API on corporate career portals | urllib (stdlib only) |
| `smartextract.py` | AI-powered scraping for arbitrary job sites via Playwright + LLM | httpx, playwright, beautifulsoup4, LLM client |

Public entry points (from `__init__.py`):
```python
from applypilot_discovery import run_discovery, run_workday_discovery, run_smart_extract
```

All three share the same config and DB injected through the **runtime adapter**.

---

## Step 1 — Files to copy verbatim

Copy this entire `applypilot_discovery/` directory into the new package's `src/` tree.
Also copy these YAML config files (they ship alongside the code):

```
applypilot_discovery/
├── __init__.py
├── runtime.py          ← dependency injection layer
├── jobspy.py
├── workday.py
├── smartextract.py
└── config/             ← create this subdirectory and put these files in it
    ├── employers.yaml  ← Workday employer registry (copy from applypilot/config/employers.yaml)
    ├── sites.yaml      ← SmartExtract site registry (copy from applypilot/config/sites.yaml)
    └── searches.example.yaml  ← user config template (copy from applypilot/config/searches.example.yaml)
```

---

## Step 2 — The runtime adapter contract

All host-app dependencies are injected through one dataclass in `runtime.py`:

```python
@dataclass(slots=True)
class DiscoveryRuntime:
    load_search_config: Callable[[], dict]
    get_connection:     Callable[..., sqlite3.Connection]
    init_db:            Callable[..., sqlite3.Connection]
    get_stats:          Callable[..., dict]
    get_llm_client:     Callable[[], Any]   # only used by smartextract
    config_dir:         Path                # must contain employers.yaml + sites.yaml
```

Call `configure_runtime(runtime)` once at startup before using any discovery function.
If you never call it, it auto-builds the default ApplyPilot-backed runtime (which will
fail in a different package since it imports `applypilot.*`).

### 2a. `load_search_config() -> dict`

Returns a plain dict. The discovery code reads these keys:

```python
{
    # Used by all three scrapers
    "location_accept":            ["Remote", "San Francisco", "CA", ...],  # flat list
    "location_reject_non_remote": ["London", "India", ...],                # flat list

    # Used by jobspy.py
    "queries":   [{"query": "software engineer", "tier": 1}, ...],
    "locations": [{"location": "Remote", "remote": True}, ...],
    "defaults": {
        "results_per_site": 100,     # max results per board per query
        "hours_old":        72,      # only jobs posted within N hours
        "country_indeed":   "usa",
    },
    "sites":   ["indeed", "linkedin", "zip_recruiter"],  # optional; these are the defaults
    "proxy":   None,                 # optional "host:port:user:pass" or "host:port"
    "tiers":   None,                 # optional list[int] to filter queries
    "location_labels": None,         # optional list[str] to filter locations
    "glassdoor_location_map": {},    # optional {"San Francisco, CA": "San Francisco"}

    # Used by workday.py
    "workday_max_tier":        2,    # only use queries with tier <= this value
    "workday_location_filter": True, # whether to apply location filtering
}
```

The searches.example.yaml in the config/ folder shows a full user-facing version of this config.
Your implementation can load it from any source (YAML file, DB, env vars, hardcoded dict).

### 2b. `get_connection()` / `init_db()` → SQLite connection

`init_db()` must create the `jobs` table if it does not exist and return the connection.
`get_connection()` must return the same connection. Thread-local caching is recommended
because `workday.py` optionally runs with `ThreadPoolExecutor`.

**Minimum required schema** (discovery only writes these columns):

```sql
CREATE TABLE IF NOT EXISTS jobs (
    url               TEXT PRIMARY KEY,
    title             TEXT,
    salary            TEXT,
    description       TEXT,
    location          TEXT,
    site              TEXT,
    strategy          TEXT,         -- "jobspy" | "workday_api" | "json_ld" | "api_response" | "css_selectors"
    discovered_at     TEXT,         -- ISO-8601 UTC timestamp
    full_description  TEXT,         -- full job text (populated by workday/smartextract directly)
    application_url   TEXT,         -- direct apply URL when available
    detail_scraped_at TEXT,         -- set when full_description is populated
    detail_error      TEXT          -- error message if detail fetch failed
);
```

Duplicate URLs are silently ignored via `sqlite3.IntegrityError` on the PRIMARY KEY.
You may add any additional columns your pipeline needs — discovery only writes the above.

### 2c. `get_stats() -> dict`

Only called by `smartextract.py` for logging. Can return an empty dict or a stub if you
don't need it. No discovery logic depends on its return value.

### 2d. `get_llm_client() -> LLMClient`

Only used by `smartextract.py`. The object must implement:

```python
class LLMClient:
    def ask(self, prompt: str, temperature: float = 0.0, max_tokens: int = 4096) -> str:
        ...  # returns the model's text response

    def chat(self, messages: list[dict], temperature: float = 0.0, max_tokens: int = 4096) -> str:
        ...  # messages is OpenAI-style: [{"role": "user", "content": "..."}]
```

If you are not using `smartextract`, you can pass a stub:

```python
class _NoOpLLM:
    def ask(self, prompt, **kw): raise NotImplementedError("LLM not configured")
    def chat(self, messages, **kw): raise NotImplementedError("LLM not configured")
```

### 2e. `config_dir: Path`

A `pathlib.Path` pointing to the directory that contains `employers.yaml` and `sites.yaml`.
Set this to wherever you put those files in Step 1.

---

## Step 3 — Bootstrap (wiring it all together)

```python
# your_package/discovery_bootstrap.py

from pathlib import Path
from applypilot_discovery.runtime import configure_runtime, DiscoveryRuntime

def setup_discovery(db_path: Path, search_config: dict, llm_client=None):
    import sqlite3
    import threading

    _local = threading.local()

    def get_connection():
        if not hasattr(_local, "conn"):
            _local.conn = sqlite3.connect(str(db_path))
            _local.conn.execute("PRAGMA journal_mode=WAL")
        return _local.conn

    def init_db():
        conn = get_connection()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                url               TEXT PRIMARY KEY,
                title             TEXT,
                salary            TEXT,
                description       TEXT,
                location          TEXT,
                site              TEXT,
                strategy          TEXT,
                discovered_at     TEXT,
                full_description  TEXT,
                application_url   TEXT,
                detail_scraped_at TEXT,
                detail_error      TEXT
            )
        """)
        conn.commit()
        return conn

    configure_runtime(DiscoveryRuntime(
        load_search_config = lambda: search_config,
        get_connection     = get_connection,
        init_db            = init_db,
        get_stats          = lambda: {},
        get_llm_client     = lambda: llm_client,
        config_dir         = Path(__file__).parent / "config",
    ))
```

Then at startup:

```python
from your_package.discovery_bootstrap import setup_discovery
from applypilot_discovery import run_discovery, run_workday_discovery, run_smart_extract

setup_discovery(
    db_path=Path("~/.yourapp/jobs.db").expanduser(),
    search_config={
        "queries": [{"query": "software engineer", "tier": 1}],
        "locations": [{"location": "Remote", "remote": True}],
        "location_accept": ["Remote", "United States"],
        "location_reject_non_remote": ["India", "London"],
        "defaults": {"results_per_site": 50, "hours_old": 72, "country_indeed": "usa"},
    },
    llm_client=your_llm_client,  # or None if not using smartextract
)

run_discovery()                      # JobSpy — job boards
run_workday_discovery(workers=4)     # Workday — corporate ATS
run_smart_extract()                  # AI scraper — arbitrary sites
```

---

## Step 4 — Dependencies

Add to the new package's `pyproject.toml`:

```toml
[project]
dependencies = [
    # Required for all discovery
    "pyyaml>=6.0",
    "python-jobspy",      # jobspy scraper
    "pandas>=2.0",        # jobspy returns DataFrames

    # Required for smartextract only (can omit if not using it)
    "httpx>=0.24",
    "beautifulsoup4>=4.12",
    "playwright>=1.40",
]
```

After install, run once to download the browser binary:
```bash
playwright install chromium
```

---

## Quick reference: what each scraper reads from config

| Config key | jobspy | workday | smartextract |
|---|---|---|---|
| `queries` | ✓ | ✓ (filtered by tier) | ✓ (for URL placeholders) |
| `locations` | ✓ | — | ✓ (for URL placeholders) |
| `location_accept` | ✓ | ✓ | ✓ |
| `location_reject_non_remote` | ✓ | ✓ | ✓ |
| `defaults.results_per_site` | ✓ | — | — |
| `defaults.hours_old` | ✓ | — | — |
| `defaults.country_indeed` | ✓ | — | — |
| `sites` (boards list) | ✓ | — | — |
| `proxy` | ✓ | ✓ | — |
| `workday_max_tier` | — | ✓ | — |
| `workday_location_filter` | — | ✓ | — |
| `glassdoor_location_map` | ✓ | — | — |
| `config_dir/employers.yaml` | — | ✓ | — |
| `config_dir/sites.yaml` | — | — | ✓ |

---

## Notes for the refining agent

- **Do not modify** `jobspy.py`, `workday.py`, or `smartextract.py` logic — they are self-contained.
- All internal imports are **relative** (`from .runtime import ...`), so the package works
  regardless of what top-level name it is installed under.
- `runtime.py` raises a clear `RuntimeError` if `configure_runtime()` was never called and
  `applypilot` is not installed — no silent crash, no confusing ImportError.
- All three scrapers are safe to call independently. You don't have to use all three.
- `workday.py` has a `workers` parameter for parallel employer scraping via `ThreadPoolExecutor`.
  The DB connection must be thread-safe (WAL mode + thread-local connections handles this).
- `smartextract.py` also accepts `workers` for parallel site processing.
