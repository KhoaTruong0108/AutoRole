# AutoRole SnapFlow Migration Plan

## 1. Objective

Migrate AutoRole from the legacy queue-worker pipeline in `src/autorole/job_pipeline.py` to a fully isolated SnapFlow-native runtime based on:

1. SnapFlow topology + runner
2. SnapFlow-provided queue, store, and TUI surfaces from the checked-in package at `workspace/SnapFlow`
3. SnapFlow store adapter subclassing for AutoRole domain persistence
4. Seeder-driven run creation for exploration
5. Stage-by-stage vertical slices with hard verification gates between slices

This migration is a replacement path, not an in-place evolution. The new runtime must live in a separate directory tree with no operational dependency on the legacy worker modules. The old path will be deprecated and removed after cutover.

The checked-in SnapFlow package at `workspace/SnapFlow` is the authoritative implementation reference for runtime structure, adapter contracts, queue/store behavior, and TUI usage. The current app under `src/autorole/` is behavioral reference material only. Its classes, workers, queue abstractions, and orchestration types must not be imported, subclassed, wrapped, instantiated, or type-referenced by the new runtime.

The migration order is fixed:

1. New directory and runtime structure
2. Refine AutoRole database table strategy
3. Migrate exploring into a new seeder
4. Migrate scoring
5. Migrate tailoring
6. Migrate packaging and session
7. Migrate `form_intelligence` to `formScraper`
8. Migrate `llm_field_completer` to `fieldCompleter`
9. Migrate `form_submission`
10. Migrate concluding

Each slice must be complete before the next starts. Completion means:

1. Manual test passes
2. TUI support is visible and usable through SnapFlow's existing screens
3. Database integrity checks pass across SnapFlow runtime tables and AutoRole domain tables
4. Old path for that slice is not required by the new slice and is either frozen at the entrypoint boundary, used only as a behavior oracle, or removed

## 2. Migration Principles

### 2.1 What stays stable

1. `JobApplicationContext` remains the business payload until a later cleanup pass
2. SQLite remains the primary migration backend for local verification
3. Listing identity and idempotency rules remain intact after schema consolidation
4. `correlation_id` becomes the only canonical cross-table join key in the new runtime

### 2.2 Isolation and non-reuse rule

1. New runtime code must live under an isolated tree and must not import legacy queue workers as runtime dependencies
2. `src/autorole/` is reference material for current behavior only; it must not contribute runtime classes, adapters, executors, gates, stores, queues, or TUI components to `src/autorole_next/`
3. The new runtime must not import, subclass, wrap, instantiate, or type-reference any class defined under `src/autorole/`
4. If behavior must be preserved, reimplement it directly against SnapFlow interfaces using the checked-in package at `workspace/SnapFlow` as the structural source of truth
5. Legacy modules under `src/autorole/workers/`, `src/autorole/queue/`, and `src/autorole/job_pipeline.py` are comparison material during migration, not part of the target architecture
6. The final system must be operable even if the legacy runtime package subtree is deleted

### 2.3 SnapFlow-first rule

1. All runtime pipeline, queue, and DLQ persistence must use the database tables created and consumed by SnapFlow adapters from `workspace/SnapFlow`
2. For the current checked-in SnapFlow package, that means using `pipeline_runs`, `pipeline_contexts`, `queue_messages`, and `dlq_messages` as framework-owned runtime tables instead of introducing AutoRole-specific substitutes
3. If the selected SnapFlow revision exposes additional framework-owned stage or gate runtime tables, those tables must be adopted directly rather than recreated in `autorole_next`
4. TUI usage must reuse SnapFlow's existing screens and provider flow instead of building a parallel AutoRole-only TUI surface
5. Stage executors, gates, seeders, queue adapters, and store adapters in `autorole_next` must follow the structure and behavior expected by SnapFlow interfaces and runtime lifecycle

### 2.4 What changes first

1. Queue orchestration moves from `src/autorole/job_pipeline.py` to SnapFlow `Topology` + `PipelineRunner`
2. Persistence ownership moves from ad hoc repository writes plus checkpoint semantics to SnapFlow queue/store adapters plus an AutoRole store subclass for domain projections
3. Exploration becomes a `PipelineSeeder` concern, not a normal downstream worker stage
4. Stage routing becomes gate-driven in SnapFlow rather than queue-name-driven inside the legacy worker layer
5. All new persistence contracts move from `run_id` to `correlation_id`
6. Existing SnapFlow CLI and TUI entrypoints are used for runtime inspection instead of creating separate AutoRole runtime screens

## 3. Target Directory Structure

Create a dedicated migration surface that is operationally isolated from the legacy runtime.

```text
src/autorole_next/
  __init__.py
  app.py                     # build runner, config, and startup entrypoints
  topology.py                # canonical AutoRole SnapFlow topology
  stage_ids.py               # stable stage names and rename map
  store.py                   # AutoRoleStoreAdapter extends SQLiteStoreAdapter
  payloads.py                # typed seed payloads and transition payload helpers
  schema.py                  # domain table definitions and row mappers
  seeders/
    __init__.py
    exploring.py             # PipelineSeeder-based discovery/manual-url/url-file seeder
  gates/
    __init__.py
    scoring.py               # pass/loop/block logic for scoring/tailoring
    form_flow.py             # pass/loop/block logic for form scraper/submission
  cli/
    __init__.py
    run.py                   # run/seed/dev commands bound to topology
    verify.py                # db integrity checks and slice smoke commands
  adapters/
    __init__.py
    stage_adapters.py        # SnapFlow executors/gates implemented for AutoRole behavior without legacy class reuse
```

Supporting test layout:

```text
tests/integration/autorole_next/
  test_exploring_seeder.py
  test_scoring_slice.py
  test_tailoring_slice.py
  test_packaging_session_slice.py
  test_form_scraper_slice.py
  test_field_completer_slice.py
  test_form_submission_slice.py
  test_concluding_slice.py

tests/unit/autorole_next/
  test_store_adapter.py
  test_topology.py
  test_scoring_gate.py
  test_form_flow_gate.py
  test_stage_name_aliases.py
```

Rationale:

1. The SnapFlow handbook recommends subclassing the store adapter instead of editing framework internals
2. The checked-in package at `workspace/SnapFlow` already defines the runner lifecycle, SQLite queue/store behavior, and reusable TUI screens that the migration should consume directly
3. Seeder utilities should own run creation and observability metadata
4. TUI support should be treated as first-class migration scope, not a late add-on, but through SnapFlow's existing screens rather than a parallel AutoRole UI
5. Physical isolation makes it possible to delete the old runtime cleanly at the end of the migration

## 4. Database Refinement Plan

The current schema in `src/autorole/db/migrations/001_domain.sql` was designed around the legacy pipeline. SnapFlow should own runtime state; the new AutoRole schema should own only the shortest useful domain projections.

### 4.1 Separate runtime tables from domain tables

SnapFlow runtime tables from `workspace/SnapFlow` should become the source of truth for:

1. run status
2. persisted context snapshots
3. queue state
4. DLQ state
5. worker-visible stage transitions

For the current checked-in SnapFlow package, this means using the framework-owned `pipeline_runs`, `pipeline_contexts`, `queue_messages`, and `dlq_messages` tables directly. No AutoRole-local replacement for those runtime tables should be introduced. If a later SnapFlow revision adds framework-owned stage or gate runtime tables, those must also be used directly rather than recreated in `autorole_next`.

AutoRole tables should remain focused on minimal domain outputs:

1. `listings`
2. `score_reports`
3. `tailored_resumes`
4. `sessions`
5. `applications`

`pipeline_checkpoints` should be deprecated once SnapFlow run context persistence is confirmed equivalent.

### 4.2 Introduce an AutoRole store adapter

Create `AutoRoleStoreAdapter(SQLiteStoreAdapter)` with responsibilities:

1. Extend SnapFlow's `SQLiteStoreAdapter` without bypassing its runtime schema initialization behavior
2. Create and migrate AutoRole domain projection tables in `_ensure_initialized`
3. Provide domain upsert helpers that replace legacy repository behavior without reusing legacy repository classes
4. Expose integrity-check helpers for manual verification commands
5. Preserve idempotent writes for repeated seeding, retries, and DLQ redrive
6. Project all domain tables using `correlation_id` as the join key

### 4.3 Consolidate and shorten the schema before stage migration

Before migrating exploring, make these schema decisions explicit:

1. `run_id` is deprecated in the new runtime and replaced by `correlation_id`
2. `job_listings` and `listing_identities` are consolidated into a single `listings` table
3. `listings.canonical_key` remains the dedupe key across re-seeds
4. `score_reports` remains append-only per attempt but joins through `correlation_id`
5. `applications` remains the terminal projection table
6. `pipeline_checkpoints` becomes read-only compatibility data during migration, then removable
7. No new AutoRole table may shadow or duplicate SnapFlow pipeline, queue, stage, or DLQ runtime tables

Recommended shortened domain schema:

```text
listings
  correlation_id TEXT PRIMARY KEY
  canonical_key  TEXT NOT NULL UNIQUE
  source_name    TEXT
  source_meta    TEXT
  job_url        TEXT NOT NULL
  apply_url      TEXT
  company_name   TEXT NOT NULL
  job_title      TEXT NOT NULL
  external_job_id TEXT
  platform       TEXT NOT NULL
  discovered_at  TEXT NOT NULL
  updated_at     TEXT NOT NULL

score_reports
  id             INTEGER PRIMARY KEY AUTOINCREMENT
  correlation_id TEXT NOT NULL
  attempt        INTEGER NOT NULL
  overall_score  REAL NOT NULL
  criteria_json  TEXT
  matched_json   TEXT
  mismatched_json TEXT
  jd_summary     TEXT
  created_at     TEXT NOT NULL

tailored_resumes
  id             INTEGER PRIMARY KEY AUTOINCREMENT
  correlation_id TEXT NOT NULL
  attempt        INTEGER NOT NULL
  resume_path    TEXT NOT NULL
  diff_summary   TEXT
  tailoring_degree INTEGER
  created_at     TEXT NOT NULL

sessions
  correlation_id TEXT PRIMARY KEY
  platform       TEXT NOT NULL
  authenticated  INTEGER NOT NULL
  session_note   TEXT
  created_at     TEXT NOT NULL
  updated_at     TEXT NOT NULL

applications
  correlation_id TEXT PRIMARY KEY
  status         TEXT
  confirmed      INTEGER
  applied_at     TEXT
  resume_path    TEXT
  pdf_path       TEXT
  final_score    REAL
  created_at     TEXT NOT NULL
  updated_at     TEXT NOT NULL
```

This schema deliberately removes duplicated identity storage, repeated foreign-key fanout through multiple semantic keys, and legacy-only columns that are already present in the SnapFlow context store.

### 4.4 Add integrity checks

Create verification commands that assert:

1. every terminal `applications.correlation_id` has a corresponding SnapFlow run record
2. every `score_reports.correlation_id`, `tailored_resumes.correlation_id`, and `sessions.correlation_id` resolves to an existing listing and run
3. every `listings.correlation_id` resolves to exactly one SnapFlow run
4. no slice writes duplicate terminal application records for the same `canonical_key`
5. required SnapFlow runtime tables exist and are actively used: `pipeline_runs`, `pipeline_contexts`, `queue_messages`, and `dlq_messages`
6. no new table in `autorole_next` depends on `run_id`

## 5. Vertical Slice Execution Model

For each slice, use the same execution contract:

1. wire stage into SnapFlow topology
2. run manual seed against SQLite database
3. inspect run in SnapFlow CLI and the existing SnapFlow TUI screens
4. run integrity verification command
5. compare behavior against `src/autorole/` only as a reference oracle
6. remove or shim the legacy equivalent only after parity is proven

Definition of done for every slice:

1. Stage can run through SnapFlow without the legacy queue worker for that step
2. Inputs and outputs are persisted in the new store path
3. SnapFlow TUI shows the stage with the correct label and state transitions without AutoRole-only replacement screens
4. Retry, loop, or block behavior is preserved where applicable
5. Manual operator instructions are documented in the slice PR or doc update
6. The slice runs without importing or type-referencing the deprecated legacy runtime path

## 6. Ordered Migration Slices

### Slice 0. Runtime foundation

Scope:

1. Create `src/autorole_next/` structure
2. Add topology builder, stage id constants, store adapter skeleton, CLI entrypoints, TUI wiring hooks
3. Establish a clean reimplementation boundary so the new runtime does not depend on legacy workers or classes
4. Bind AutoRole to SnapFlow's existing TUI screens and runtime table layout rather than creating parallel infrastructure

Deliverables:

1. `build_topology()` returns a valid SnapFlow topology for a minimal stub flow
2. `AutoRoleStoreAdapter` initializes SnapFlow runtime tables and shortened AutoRole domain tables
3. `snapflow` CLI can start a worker for a selected AutoRole stage
4. SnapFlow TUI can open against the AutoRole SQLite file using the framework's existing screens
5. No `autorole_next` runtime module imports or wraps a class from `src/autorole`

Manual verification:

1. Start SnapFlow worker for one stub stage
2. Seed one synthetic payload
3. Confirm `pipeline_runs`, `pipeline_contexts`, `queue_messages`, and `dlq_messages` behavior plus TUI visibility

### Slice 1. Database refinement

Scope:

1. Move persistence ownership toward `AutoRoleStoreAdapter`
2. Consolidate `job_listings` and `listing_identities` into `listings`
3. Replace `run_id` with `correlation_id` across the new schema and APIs
4. Add integrity verification CLI

Deliverables:

1. Schema migration for the new shortened projection tables and indexes required by SnapFlow without duplicating framework runtime tables
2. Store adapter methods that replace direct repository calls without reusing `src/autorole` repository classes
3. Integrity report command with clear pass/fail output
4. Compatibility read-path for legacy rows only if needed for data backfill or audit

Manual verification:

1. Run DB initialization from a clean SQLite file
2. Run integrity checker on empty and populated DBs
3. Confirm the new runtime works without importing legacy runtime modules
4. If data backfill is needed, validate one-way migration from old tables into the new consolidated schema
5. Confirm SnapFlow CLI and TUI still operate against the same SQLite file after domain schema creation

### Slice 2. Exploring becomes seeder

Scope:

1. Remove exploring from the normal worker chain
2. Build `PipelineSeeder`-based seeding using SnapFlow's seeder contract for:
   - search discovery
   - manual single URL
   - JSON URL list
3. Persist listing identity decisions into the consolidated `listings` table at seed time

Deliverables:

1. `seeders/exploring.py` emits one SnapFlow run per `ExplorationSeed`
2. Source metadata is preserved for observability
3. Seeder is idempotent against duplicate listings via `listings.canonical_key`
4. Seeder implementation follows SnapFlow runner and metadata flow without borrowing legacy exploring classes

Manual verification:

1. Seed from search configuration
2. Seed from manual URL
3. Seed from URL list file
4. Confirm duplicate seeds do not create duplicate application runs
5. Confirm each seeded run has a stable `correlation_id` used across all subsequent tables

TUI check:

1. Runs appear as seeded runs before downstream stages start
2. Source metadata is inspectable from stored context or run metadata through SnapFlow's existing TUI screens

DB integrity check:

1. `listings` remains unique on `canonical_key` and stable on `correlation_id` across repeated seeding

### Slice 3. Migrate scoring

Scope:

1. Register `scoring` as the first real worker-backed stage in SnapFlow
2. Replace legacy queue routing with a SnapFlow gate that preserves pass/loop/block semantics if scoring still owns the decision
3. Persist score attempts through the store adapter
4. Implement the stage as a SnapFlow executor/gate pair rather than a wrapper over legacy worker classes

Deliverables:

1. SnapFlow `StageNode` for scoring
2. Scoring gate wrapper for next-stage routing
3. Attempt-aware score persistence

Manual verification:

1. Run one qualifying listing that passes
2. Run one listing that loops or blocks under current threshold logic
3. Confirm retry behavior uses the latest context payload, not the original seed payload

TUI check:

1. scoring state transitions and attempts are visible in SnapFlow's Stage Monitor and Run Inspector screens

DB integrity check:

1. `score_reports` rows match attempts shown in SnapFlow run history and join to `listings` by `correlation_id`

### Slice 4. Migrate tailoring

Scope:

1. Move tailoring behind SnapFlow as the next stage after scoring
2. Preserve the scoring-tailoring loop contract if the scoring gate still evaluates after tailoring
3. Store tailored resume artifacts and diff summaries through the new adapter path
4. Implement tailoring through SnapFlow executor/gate contracts only

Deliverables:

1. Tailoring `StageNode`
2. Scoring loop gate finalized in SnapFlow
3. Artifact references stored so TUI and run inspection can locate outputs

Manual verification:

1. Pass case with tailored resume output
2. Loop case where tailoring feeds a new scoring attempt
3. Resume a run from tailoring after a controlled failure

TUI check:

1. Tailoring artifacts and attempts are inspectable through SnapFlow's existing TUI screens

DB integrity check:

1. `tailored_resumes` rows match SnapFlow artifact refs and `correlation_id`

### Slice 5. Migrate packaging and session

Scope:

1. Migrate packaging and session together because session depends on packaged materials and authenticated browser context setup
2. Preserve current packaging output paths and session persistence semantics during the slice
3. Implement both stages as native SnapFlow executors rather than wrappers over legacy worker classes

Deliverables:

1. Packaging `StageNode`
2. Session `StageNode`
3. Store adapter projection methods for packaged outputs and session establishment

Manual verification:

1. Run one full slice through packaging and session
2. Confirm packaged artifacts exist on disk and in run metadata
3. Confirm session record can be inspected after rerun or restart

TUI check:

1. Packaging and session appear as distinct stages with successful output state in SnapFlow's existing screens

DB integrity check:

1. `sessions` points to the same `correlation_id` as packaged artifacts and listing records

### Slice 6. Migrate `form_intelligence` to `formScraper`

Scope:

1. Rename the stage id and user-facing label to `formScraper`
2. Keep a compatibility alias from `form_intelligence` during migration
3. Move form page discovery and extraction into the renamed SnapFlow stage
4. Implement the stage through SnapFlow interfaces instead of reusing legacy `form_intelligence` classes

Deliverables:

1. Stage id alias map in `stage_ids.py`
2. `formScraper` node replaces `form_intelligence` in the topology
3. Legacy tests are updated or shimmed until renamed tests land

Manual verification:

1. Run against a page with a supported apply form
2. Run against a page requiring loop-back from submission
3. Confirm stored payload contains extracted form fields and page metadata

TUI check:

1. SnapFlow TUI shows `formScraper` while still understanding historic `form_intelligence` runs

DB integrity check:

1. Any persisted form extraction data is tied to the same run and not duplicated under both names

### Slice 7. Migrate `llm_field_completer` to `fieldCompleter`

Scope:

1. Rename the stage id to `fieldCompleter`
2. Keep a temporary compatibility alias for existing fixtures and test inputs
3. Route `formScraper` output directly into `fieldCompleter`
4. Implement the stage through SnapFlow interfaces instead of reusing legacy `llm_field_completer` classes

Deliverables:

1. Renamed SnapFlow stage node
2. Compatibility handling for old payload keys only where strictly required
3. Output payload contract documented for submission stage

Manual verification:

1. Complete at least one form payload with LLM-assisted values
2. Re-run the same case to confirm deterministic persistence boundaries

TUI check:

1. field completion step is separately visible and debuggable in SnapFlow's existing screens

DB integrity check:

1. No duplicate field completion artifacts are written for one run unless a retry actually occurred

### Slice 8. Migrate form submission

Scope:

1. Move submission into SnapFlow with a dedicated form-flow gate
2. Preserve pass/loop/block behavior
3. Ensure dry-run and apply modes still map cleanly to operator workflows
4. Implement submission as a native SnapFlow stage/gate path rather than a legacy wrapper

Deliverables:

1. Form submission `StageNode`
2. Gate that routes loop-back to `formScraper`
3. Consistent audit-log artifact references in run context

Manual verification:

1. Dry-run path
2. Submit-disabled or guardrail path
3. Loop-back path requiring another scrape/extract cycle
4. Successful submit path

TUI check:

1. Submission outcomes and loop reasons are visible in SnapFlow Run Inspector and Stage Monitor

DB integrity check:

1. `applications.status` is only updated by the canonical submission path

### Slice 9. Migrate concluding

Scope:

1. Move final projection and terminal run handling into SnapFlow
2. Remove dependence on the legacy concluding worker
3. Finalize job application persistence through the store adapter
4. Implement concluding as a native SnapFlow stage rather than a legacy wrapper

Deliverables:

1. Concluding `StageNode`
2. Terminal projection updates written by `AutoRoleStoreAdapter`
3. End-to-end run completion visible in SnapFlow CLI and TUI
4. No runtime class dependency remains on `src/autorole`

Manual verification:

1. Successful end-to-end run
2. Blocked or failed end-to-end run with terminal visibility
3. Redrive from DLQ where applicable

TUI check:

1. Terminal run state and final artifacts are visible

DB integrity check:

1. `applications` row matches SnapFlow terminal state and final context snapshot by `correlation_id`

## 7. Compatibility and Cutover Rules

During migration:

1. one stage family is migrated at a time
2. the next stage is not migrated until the previous slice has passed manual, TUI, and DB checks
3. stage renames use aliases until all tests, fixtures, and docs move over
4. the legacy queue worker path is never a dependency of the new runtime; it may exist only as a behavior reference and deprecated fallback entrypoint during rollout
5. no class from `src/autorole/` may be imported into `src/autorole_next/`, even temporarily
6. the SnapFlow package at `workspace/SnapFlow` is the only approved structural reference for runner, adapter, seeder, CLI, and TUI behavior

After concluding is complete:

1. remove legacy queue topology code from `src/autorole/job_pipeline.py`
2. remove stage-specific workers in `src/autorole/workers/` that are fully superseded
3. deprecate `pipeline_checkpoints`
4. update all operator docs and scripts to SnapFlow entrypoints only
5. remove remaining legacy `run_id` joins from the domain schema and codebase

## 8. Verification Matrix Per Slice

Use the same checklist every time:

1. Code verification
    - topology loads
    - selected worker starts
    - stage can process one seeded run
    - no runtime import or type reference from `src/autorole/` appears under `src/autorole_next/`
2. Manual verification
    - one happy-path scenario
    - one failure or loop scenario if the stage supports it
3. TUI verification
    - SnapFlow TUI screens are used unchanged for stage monitor, run inspection, queue, and DLQ views
    - stage visible by final name
    - run detail shows attempt and artifacts
4. Database verification
    - SnapFlow runtime tables exist and are used directly
    - `pipeline_runs`, `pipeline_contexts`, `queue_messages`, and `dlq_messages` remain authoritative
    - projection rows written exactly once per expected attempt
    - `correlation_id` joins pass across all domain tables
    - no duplicate terminal records for a canonical listing
    - no new schema object relies on `run_id`
5. Regression verification
    - relevant unit tests
    - relevant integration slice tests

No slice advances until every item above is green.

## 9. Recommended Implementation Order Inside Each Slice

For each migration slice, follow this micro-order:

1. add store/topology contracts first
2. implement SnapFlow-native executors, gates, and seeder/store subclasses second
3. add CLI and TUI visibility third using SnapFlow's existing entrypoints and screens
4. add integrity checks fourth
5. run manual test last

This keeps the migration honest: the stage is not considered done just because the executor runs.

## 10. First Execution Sprint

The first sprint should stop after Slice 2 completes. That means the immediate implementation target is:

1. create `src/autorole_next/`
2. build `AutoRoleStoreAdapter`
3. stand up the base topology
4. migrate exploring into a real seeder
5. bind the runtime to SnapFlow's existing TUI screens and runtime tables
6. add manual verification and DB integrity commands

Do not start scoring until the seeder path is stable, idempotent, and observable in the TUI.