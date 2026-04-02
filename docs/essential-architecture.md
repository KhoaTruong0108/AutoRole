# AutoRole Essentials

This document is the shortest complete mental model of the project: core entities, relationships, contracts, flow, and how to run it.

## 1) What This System Does

AutoRole is an event-driven pipeline that takes job listings and moves each listing through application stages:

exploring -> scoring -> tailoring -> packaging -> session -> form_intelligence -> llm_field_completer -> form_submission -> concluding

Each stage reads from one queue, writes to the next queue, persists checkpoint/state, and can loop or dead-letter on failure.

## 2) Core Domain Entities

Defined in src/autorole/context.py.

- JobListing
  - A discovered listing (url, company, platform, title, ids).
- JobApplicationContext
  - The canonical payload passed stage-to-stage.
  - Contains progressively filled fields: listing, score, tailored, packaged, session, form_intelligence, llm_field_completion, form_session, applied.
- ScoreReport
  - JD analysis and score outputs.
- TailoredResume
  - Tailored resume artifact + summary.
- PackagedResume
  - Final PDF artifact for submission.
- FormSession
  - Browser/form state across pages (fields, instructions, outcomes, screenshots).
- ApplicationResult
  - Final submission metadata/status for concluding.

## 3) Persistence Entities (SQLite)

From migrations in src/autorole/db/migrations/001_domain.sql and src/autorole/db/migrations/002_queue.sql.

- resumes
  - Resume files (master and generated).
- job_listings
  - Primary listing row keyed by run_id.
- listing_identities
  - Canonical identity dedup key for listing-level uniqueness.
- score_reports
  - Per-run scoring outputs, with attempts.
- tailored_resumes
  - Tailoring output artifacts per run.
- session_records
  - Session/auth state snapshots.
- job_applications
  - Final application lifecycle row (status, score, applied_at).
- pipeline_checkpoints
  - Last successful stage + full serialized context per run.
- queue_messages
  - Event bus table: queued/processing messages, visibility timeout, metadata, attempts.

## 4) Relationship Model (Essential)

- run_id is the backbone identifier across queue messages, checkpoints, and domain records.
- job_listings/run_id anchors most run-scoped persistence.
- pipeline_checkpoints/run_id stores recoverable context_json for resume/restart.
- queue_messages/run_id carries a run through stage queues.
- listing_identities/canonical_key prevents duplicate processing at listing identity level.

Practical cardinality:
- One run_id -> one evolving JobApplicationContext
- One run_id -> many queue_messages over time (including retries/loops)
- One run_id -> zero/one current checkpoint
- One run_id -> one final job_applications row (after concluding)

## 5) Queue Topology and Stage Mapping

Queue constants in src/autorole/queue/backend.py.

- exploring_q -> scoring_q
- scoring_q -> tailoring_q
- tailoring_q -> packaging_q
- packaging_q -> session_q
- session_q -> form_intel_q
- form_intel_q -> llm_field_completer_q
- llm_field_completer_q -> form_sub_q
- form_sub_q -> concluding_q
- dead_letter_q for blocked messages

This same mapping is enforced in:
- src/autorole/workers/base.py
- src/autorole/job_pipeline.py
- src/autorole/workers/devrun.py

## 6) Runtime Contracts

### 6.1 Queue Message Contract

Defined by Message in src/autorole/queue/backend.py.

Required fields:
- message_id
- run_id
- stage
- payload (dict, usually JobApplicationContext JSON)
- reply_queue
- dead_letter_queue
- attempt
- metadata

Important metadata keys:
- run_mode: observe | apply | apply-dryrun
- dryrun_stop_after_submit: form submission dryrun behavior
- __exec_attempt: internal exception retry counter
- __loop_attempt: internal routing loop counter

### 6.2 Stage Execution Contract

Workers execute stage.execute(msg) and expect a StageResult-like response:
- success true + output payload on pass
- success false + error on stage failure
- unhandled exception treated as retryable execution failure

### 6.3 Routing Contract

Implemented in src/autorole/workers/base.py via RoutingDecision:
- pass: enqueue enriched output to reply_queue
- loop: re-enqueue to loop queue (usually same input queue)
- block: move to dead_letter_q

Retry policy (essential behavior):
- Exceptions retry on same queue until max attempts.
- Loop decisions retry until max attempts.
- Terminal failures are acked from input queue and moved to dead_letter_q.

## 7) End-to-End Flow

### Happy path
1. Seed is enqueued into exploring_q.
2. Each worker pulls, executes, persists checkpoint, and emits to next queue.
3. concluding persists final status into job_applications.

### Failure path
1. Stage returns failure or throws.
2. Worker retries (exception/loop cases) or blocks.
3. On block, message is sent to dead_letter_q.
4. Message can be redriven from DLQ via CLI.

### Resume path
1. System reads pipeline_checkpoints for run_id.
2. Restart from requested stage.
3. Continue queue-driven processing from that point.

## 8) Invocation (Most Useful Commands)

All commands assume repository root and typically PYTHONPATH=src.

### 8.1 Full real pipeline
- Observe:
  - PYTHONPATH=src python3 scripts/run_real_pipeline.py --mode observe --platforms linkedin,indeed --keywords "python,backend" --location "United States" --max-listings 1
- Apply:
  - PYTHONPATH=src python3 scripts/run_real_pipeline.py --mode apply --platforms linkedin,indeed --keywords "python,backend" --location "United States" --max-listings 1
- Apply dryrun:
  - PYTHONPATH=src python3 scripts/run_real_pipeline.py --mode apply-dryrun --job-url "<url>" --job-platform lever

### 8.2 Run one long-lived worker
- PYTHONPATH=src python3 -m autorole.workers.run --stage scoring

### 8.3 Run one stage once (dev harness)
- From fixture:
  - PYTHONPATH=src python3 -m autorole.workers.devrun --stage packaging --input-file tests/fixtures/packaging_input.json
- From queue:
  - PYTHONPATH=src python3 -m autorole.workers.devrun --stage form_submission --mode apply-dryrun --from-queue

### 8.4 Queue and DLQ operations
- Inspect queue rows:
  - PYTHONPATH=src python3 -m autorole.cli.main queue sql scoring_q --payload
- Redrive one DLQ message:
  - PYTHONPATH=src python3 -m autorole.cli.main queue redrive --message-id <message_id>
- Redrive all DLQ messages for a target queue:
  - PYTHONPATH=src python3 -m autorole.cli.main queue redrive --queue-name scoring_q

## 9) Minimal Extension Rules

If you add or rename a stage, update these together:
- stage order in src/autorole/stage_base.py
- queue constants/mapping in src/autorole/queue/backend.py
- next-queue maps in src/autorole/workers/base.py and src/autorole/job_pipeline.py
- stage to queue mapping in src/autorole/workers/devrun.py and CLI helpers
- worker build wiring in src/autorole/workers/run.py and src/autorole/job_pipeline.py
- tests for worker routing and integration flow

If only one of these is changed, flow will drift and fail in subtle ways.

## 10) One-Page Mental Model

- Canonical payload: JobApplicationContext
- Transport: queue_messages (SQLite queue)
- Execution unit: StageWorker
- Durability: pipeline_checkpoints + domain tables
- Control outcomes: pass, loop, block
- Recovery: resume from checkpoint or redrive DLQ
