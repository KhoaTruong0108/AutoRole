# LLD: Event-Driven Stage Workers

## 1. Goals & Non-Goals

### Goals
- Each stage runs as an independent worker: pull → process → route
- No shared orchestrator; stages only care about their input queue
- Failure is non-destructive: message visibility timeout returns it automatically; explicit nack on known failure
- Poll loop has a configurable idle wait to avoid CPU spinning
- Local-first: SQLite queue backend for dev/CI, interface-compatible with SQS/Redis Streams

### Non-Goals
- Distributed deployment or containerization (out of scope for this phase)
- Replacing `JobApplicationContext` or stage business logic
- Removing the e2e test pipeline (kept as integration harness; routes through the same queue backend)

---

## 2. Core Contracts

### 2.1 Message

Enhances the current `Message` dataclass with routing fields.

```
Message
  ├── message_id: str          # UUID assigned by QueueBackend.enqueue()
  ├── run_id: str              # job application run identifier
  ├── stage: str               # which stage this message targets
  ├── payload: dict            # full JobApplicationContext.model_dump()
  ├── attempt: int             # starts at 1, incremented on loop re-enqueue
  ├── reply_queue: str         # where to send the enriched message on success/pass
  ├── dead_letter_queue: str   # where to send on terminal failure
  └── metadata: dict           # gate hints (e.g. last_score_before_tailoring), loop reasons
```

`reply_queue` and `dead_letter_queue` are resolved at seed time from the queue topology config,
not hardcoded in stage logic. Stages are routing-topology-agnostic.

---

### 2.2 QueueBackend (interface)

```
QueueBackend (ABC)
  ├── enqueue(queue_name: str, message: Message) -> str
  │     Persist message; return assigned message_id.
  │     If queue doesn't exist, create it (idempotent).
  │
  ├── pull(queue_name: str, visibility_timeout_seconds: int) -> Message | None
  │     Atomically claim one message: set status=processing, set visible_after=now+timeout.
  │     Returns None if queue is empty or all messages are in-flight.
  │
  ├── ack(queue_name: str, message_id: str) -> None
  │     Hard-delete the message. Called after successful processing or DLQ routing.
  │
  ├── nack(queue_name: str, message_id: str, delay_seconds: int = 0) -> None
  │     Release the message back (set visible_after=now+delay). Worker stops processing it.
  │
  └── create_queue(queue_name: str) -> None
        Idempotent. Called at worker startup.
```

**SQLite Implementation** (`SqliteQueueBackend`):

Schema — table `queue_messages`:
```
message_id        TEXT PRIMARY KEY
queue_name        TEXT NOT NULL
run_id            TEXT NOT NULL
stage             TEXT NOT NULL
payload           TEXT NOT NULL   -- JSON
attempt           INTEGER NOT NULL DEFAULT 1
reply_queue       TEXT NOT NULL
dead_letter_queue TEXT NOT NULL
metadata          TEXT NOT NULL   -- JSON
status            TEXT NOT NULL   -- 'pending' | 'processing' | 'dead'
enqueued_at       TEXT NOT NULL   -- ISO8601
visible_after     TEXT NOT NULL   -- ISO8601; pull ignores rows where visible_after > now
```

`pull()` runs a single atomic `UPDATE … RETURNING` (or SELECT + UPDATE in a transaction) so
concurrent workers on the same SQLite file don't double-claim.

A background reaper task (runs every N seconds, configurable) resets timed-out `processing`
messages back to `pending` by setting `visible_after = now` when
`visible_after < now AND status = 'processing'`. This handles worker crashes.

---

### 2.3 RoutingPolicy (interface)

Replaces the inline gate evaluation in `_run_listing`. Each worker that has a gate owns a
`RoutingPolicy` instance; workers without gates use `PassThroughPolicy`.

```
RoutingDecision
  ├── decision: Literal["pass", "loop", "block"]
  └── reason: str

RoutingPolicy (ABC)
  └── evaluate(result: StageResult, message: Message) -> RoutingDecision

PassThroughPolicy(RoutingPolicy)
  # decision=pass if result.success, decision=block otherwise

BestFitRoutingPolicy(RoutingPolicy)
  # Wraps existing BestFitGate logic
  # loop  → re-enqueue to worker's own input_queue with attempt+1, inject score metadata
  # block → route to dead_letter_queue
  # pass  → route to reply_queue

FormPageRoutingPolicy(RoutingPolicy)
  # Wraps existing FormPageGate logic
  # loop  → re-enqueue to form_intelligence_q (not form_submission_q)
  # block → route to dead_letter_queue
  # pass  → route to reply_queue (concluding_q)
```

`inject_loop_metadata_from_gate_reason` (currently in `pipeline.py`) moves into
`BestFitRoutingPolicy.evaluate()` — it is no longer a free function.

---

### 2.4 WorkerConfig

```
WorkerConfig
  ├── input_queue: str
  ├── reply_queue: str
  ├── dead_letter_queue: str
  ├── poll_interval_seconds: float = 2.0       # idle wait between pull attempts
  ├── visibility_timeout_seconds: int = 300    # how long a pulled message stays invisible
  └── max_attempts: int = 3                    # for nack-based retry (non-gate failures)
```

`poll_interval_seconds` is the key addition: when `pull()` returns `None`, the worker sleeps
for this duration before trying again, preventing a busy-wait spin.

---

### 2.5 StageWorker (base class)

Replaces `AutoRoleStage` as the execution wrapper. Preserves all side-effect logic
(checkpoint, artifact writing) from `AutoRoleStage`.

```
StageWorker (ABC)
  name: str
  config: WorkerConfig
  routing_policy: RoutingPolicy      # injected; default PassThroughPolicy

  # ── Lifecycle ──────────────────────────────────────────────────────────────

  async run_forever(queue: QueueBackend) -> None
    """Main worker loop. Runs until cancelled."""
    while True:
        msg = await queue.pull(config.input_queue, config.visibility_timeout_seconds)
        if msg is None:
            await asyncio.sleep(config.poll_interval_seconds)   # ← configurable idle wait
            continue
        await _process(queue, msg)

  async _process(queue: QueueBackend, msg: Message) -> None
    result = await _execute_inner(msg)

    if result is None:
        # Unhandled exception in stage: nack with backoff, worker stays alive
        delay = _backoff(msg.attempt)
        await queue.nack(config.input_queue, msg.message_id, delay)
        logger.exception(...)
        return

    routing = routing_policy.evaluate(result, msg)

    match routing.decision:
        case "pass":
            enriched = _enrich(msg, result.output)
            await queue.enqueue(msg.reply_queue, enriched)
            await queue.ack(config.input_queue, msg.message_id)
            await on_success(JobApplicationContext.model_validate(result.output), msg.attempt)

        case "loop":
            loop_msg = _build_loop_message(msg, routing.reason)   # attempt+1, metadata updated
            await queue.enqueue(config.input_queue, loop_msg)
            await queue.ack(config.input_queue, msg.message_id)

        case "block":
            await queue.enqueue(msg.dead_letter_queue, msg)
            await queue.ack(config.input_queue, msg.message_id)
            logger.warning("blocked run_id=%s reason=%s", msg.run_id, routing.reason)

  # ── Preserved from AutoRoleStage ───────────────────────────────────────────

  async on_success(ctx: JobApplicationContext, attempt: int) -> None   # abstract
  def log_ok(ctx: JobApplicationContext, attempt: int) -> None         # abstract
  async _execute_inner(msg: Message) -> StageResult | None
  def _write_artifact(filename, content, run_id) -> Path
  def _append_stage_index(run_dir, filename) -> None

  # ── Helpers ────────────────────────────────────────────────────────────────

  def _enrich(msg: Message, output: dict) -> Message
    """Return new Message with payload=output, same routing fields."""

  def _build_loop_message(msg: Message, reason: str) -> Message
    """Return new Message with attempt+1, metadata updated with reason."""

  def _backoff(attempt: int) -> int
    """Exponential backoff in seconds: min(2^attempt, 60)"""
```

---

## 3. Queue Topology

```
[seed]
  │ enqueue(exploring_q)
  ▼
┌─────────────────────────────────────────────────────────────┐
│ exploring_q  →  ExploringWorker                             │
│                 fanout: one enqueue(scoring_q) per listing  │
└─────────────────────────────────────────────────────────────┘
                        │ N messages
                        ▼
┌─────────────────────────────────────────────────────────────┐
│ scoring_q    →  QualificationWorker                         │
│                 (scoring stage then tailoring stage, inline) │
│                 routing: BestFitRoutingPolicy               │
│                   loop  → re-enqueue scoring_q              │
│                   block → dead_letter_q                     │
│                   pass  → enqueue(packaging_q)              │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│ packaging_q  →  PackagingWorker  →  enqueue(session_q)      │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│ session_q    →  SessionWorker  →  enqueue(form_intel_q)     │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│ form_intel_q →  FormIntelligenceWorker  →  enqueue(form_sub_q) │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│ form_sub_q   →  FormSubmissionWorker                        │
│                 routing: FormPageRoutingPolicy              │
│                   loop  → re-enqueue form_intel_q           │
│                   block → dead_letter_q                     │
│                   pass  → enqueue(concluding_q)             │
└─────────────────────────────────────────────────────────────┘
                        │
                        ▼
┌─────────────────────────────────────────────────────────────┐
│ concluding_q →  ConcludingWorker  →  ack (terminal)         │
└─────────────────────────────────────────────────────────────┘

[dead_letter_q] ← all terminal failures; inspectable via CLI
```

**Note on DB ownership per stage**: each worker is responsible for persisting only its own
output in `on_success`. No stage writes on behalf of another:

```
ExploringWorker.on_success      → upsert_listing
QualificationWorker.on_success  → upsert_score, upsert_tailored
SessionWorker.on_success        → upsert_session
ConcludingWorker.on_success     → upsert_application
all workers                     → upsert_checkpoint  (via StageWorker base)
```

See §9 for FK decision that makes this ordering non-prescriptive.

---

**Note on QualificationWorker**: `scoring` and `tailoring` are merged into one worker
(`QualificationWorker`) because `BestFitGate` evaluates the result of _both_ stages together.
Splitting them into separate queues would require `tailoring` to always route back to `scoring`
for gate evaluation — adding a queue hop with no benefit. If they ever need to run independently,
the split can be done later.

**Note on FormPageRoutingPolicy loop target**: on `loop`, `FormSubmissionWorker` re-enqueues
to `form_intel_q` (not `form_sub_q`), matching the existing behavior where the full
form-intelligence → form-submission cycle repeats per page.

---

## 4. Exploring Fanout

`ExploringWorker` has a different output shape: one input message produces N output messages.

```
ExploringWorker
  override _process():
    result = await _execute_inner(msg)
    if not result.success: nack / DLQ (same as base)

    listings: list[JobApplicationContext] = result.output
    for ctx in listings:
        child_msg = Message(
            message_id=uuid4(),
            run_id=ctx.run_id,
            stage="qualification",
            payload=ctx.model_dump(),
            attempt=1,
            reply_queue=config.reply_queue,      # packaging_q
            dead_letter_queue=config.dead_letter_queue,
            metadata={},
        )
        await queue.enqueue(config.reply_queue, child_msg)   # reply_queue = scoring_q here

    await queue.ack(config.input_queue, msg.message_id)
```

The base `_process` loop handles scalar output; `ExploringWorker` overrides for fanout.

---

## 5. Browser Lifecycle

Browser-dependent stages: `QualificationWorker` (scoring), `SessionWorker`, `FormIntelligenceWorker`, `FormSubmissionWorker`.

**Phase 1 (current)**: Workers that share browser context run in the same process.
`StageWorkerProcess` is a thin launcher that instantiates multiple workers sharing one
`BrowserContext`, each assigned a dedicated `Page`.

```
StageWorkerProcess
  ├── browser_context: BrowserContext    # shared
  ├── workers: list[StageWorker]         # each holds its own Page
  └── run() → asyncio.gather(*[w.run_forever(queue) for w in workers])
```

This is equivalent to the current `job_pipeline.py` browser management, just without the
orchestrator logic. Each worker still runs its poll loop independently.

**Phase 2 (future)**: Remote browser pool via Playwright CDP — out of scope for this LLD.

---

## 6. Entry Points

Each logical worker group gets a runnable entry point:

```
python -m autorole.workers.run --stage exploring
python -m autorole.workers.run --stage qualification
python -m autorole.workers.run --stage packaging
python -m autorole.workers.run --stage session
python -m autorole.workers.run --stage form_intelligence
python -m autorole.workers.run --stage form_submission
python -m autorole.workers.run --stage concluding
```

A seed script replaces the `job_pipeline.py` startup:

```
python -m autorole.workers.seed --job-url <url>        # manual URL mode
python -m autorole.workers.seed --search               # scrape mode
python -m autorole.workers.seed --resume-run-id <id>   # resume from checkpoint
```

`seed` writes one message directly into `exploring_q` (or the appropriate resume stage queue)
and exits. Workers are responsible for draining.

---

## 7. E2E Test Harness (preserving job_pipeline.py)

### The Problem

`job_pipeline.py` currently runs everything in-process, sequentially. Workers run forever
via `run_forever(queue)`. A naive port would make the e2e test non-terminating.

### Solution: InMemoryQueueBackend + concurrent workers + done event

`JobApplicationPipeline` is restructured as a test harness that:

1. Creates an `InMemoryQueueBackend` (no file I/O, no timeouts)
2. Instantiates all workers with `poll_interval_seconds=0` (no idle sleep — drain as fast as possible)
3. Seeds the first queue (`exploring_q` or the resume stage queue)
4. Runs all workers concurrently via `asyncio.gather()`
5. Stops when `ConcludingWorker` fires a completion event, or a configurable timeout elapses

```
JobApplicationPipeline.run()
  │
  ├── queue = InMemoryQueueBackend()
  │
  ├── done_event = asyncio.Event()
  │
  ├── workers = _build_workers(queue, done_event, ...)
  │     # same worker classes as production, just different backend + config
  │
  ├── await queue.enqueue("exploring_q", seed_message)    # or resume stage queue
  │
  ├── tasks = [create_task(w.run_forever(queue)) for w in workers]
  │
  ├── await asyncio.wait_for(done_event.wait(), timeout=pipeline_timeout)
  │
  └── cancel all tasks → gather(return_exceptions=True)
```

`ConcludingWorker.on_success()` calls `done_event.set()` when the last stage completes.
The done event is injected at construction time (optional callback); production workers
leave it as `None` and the loop simply runs until cancelled.

**Resume mode** works the same way: `_build_workers` seeds the queue for the resume stage
(e.g. `packaging_q`) with the checkpoint context instead of seeding `exploring_q`.

### InMemoryQueueBackend

Uses `asyncio.Queue` per queue name internally.

```
InMemoryQueueBackend
  ├── _queues: dict[str, asyncio.Queue[Message]]
  │
  ├── enqueue(queue_name, message) → message_id
  │     queue.put_nowait(message); return message.message_id
  │
  ├── pull(queue_name, visibility_timeout) → Message | None
  │     try: return queue.get_nowait()
  │     except QueueEmpty: return None
  │     # No visibility timeout logic needed — single process, no crashes
  │
  ├── ack(queue_name, message_id) → None
  │     No-op. asyncio.Queue already consumed the message on get_nowait().
  │
  └── nack(queue_name, message_id, delay) → None
        queue.put_nowait(message)   # re-enqueue immediately; delay ignored in-memory
```

No reaper task needed for `InMemoryQueueBackend`. Visibility timeout is a `SqliteQueueBackend`
concern only.

### Why this is correct

- **Same worker code runs in both paths.** The e2e test exercises `StageWorker._process()`,
  routing policies, and gate logic — not a separate orchestration path.
- **`poll_interval_seconds=0`** means workers yield to the event loop on each empty-queue check
  but don't sleep, so the test drains as fast as the stages can execute.
- **Termination is deterministic.** The done event fires exactly when `ConcludingWorker`
  processes its message, not on a fixed timeout.
- **Timeout is still there as a safety net.** If any stage loops indefinitely or a message
  gets stuck, `asyncio.wait_for` prevents the test from hanging forever.

### What job_pipeline.py loses

- `_run_listing` and its inline gate evaluation — deleted
- `_execute_stage` free function — deleted (absorbed into `StageWorker._execute_inner`)
- `should_run(start_stage)` — deleted (resume now works by seeding the correct queue, not by
  checking stage order in a loop)

### What job_pipeline.py keeps

- `RunConfig` dataclass — unchanged
- Browser setup (`async_playwright`, `browser_context`, per-stage pages) — unchanged
- `_build_executors` → renamed `_build_workers`, returns `StageWorker` instances instead of
  `AutoRoleStage` instances, wired to the shared `InMemoryQueueBackend`
- `make_llm_client`, `make_renderer` — unchanged
- DB init, path resolution, trace logger — unchanged

---

## 9. Database FK Decision

### Finding: PRAGMA foreign_keys is OFF (never set)

Grep across the entire codebase (`src/` and `tests/`) finds zero occurrences of
`PRAGMA` or `foreign_keys`. SQLite's default is `foreign_keys=OFF`, which means
the `REFERENCES` clauses declared in `001_domain.sql` were **never enforced at
runtime**. The FK constraints were documentation-only intent, not active guards.

### Decision: Option A — remove DB-level FKs, rely on queue ordering

The `REFERENCES job_listings(run_id)` clauses on `score_reports`, `tailored_resumes`,
`session_records`, `job_applications`, and `pipeline_checkpoints` are dropped in
migration `002`. `run_id` remains the logical join key across all tables by convention.

**Rationale:**
- Queue topology already structurally enforces ordering (exploring → qualification → …).
  Adding DB-level enforcement would be redundant and require special-casing the first
  stage to satisfy the parent row constraint before downstream stages can write.
- Dropping FKs normalizes all stages: each stage owns `on_success` for its own domain
  output only, with no awareness of upstream prerequisites.
- Since FKs were never enforced (PRAGMA OFF), this is a schema cleanup, not a
  behavioral change. No existing data or query is affected.

**Trade-off accepted**: a dangling `score_reports.run_id` with no `job_listings` parent
is possible only through direct DB manipulation or a bug that bypasses the queue.
Application-level queries already join by `run_id`; they return null gracefully.

### Migration 002

```sql
-- 002_queue.sql
-- Part 1: Drop FK declarations from domain tables.
-- SQLite does not support DROP CONSTRAINT; tables must be recreated.
-- Since PRAGMA foreign_keys was OFF, this is a no-op for existing data.
--
-- Recreate affected tables without REFERENCES clauses, preserving all
-- other column definitions and indexes. Data migration: INSERT INTO new SELECT FROM old.
--
-- Part 2: Create queue_messages table (no FK on run_id — queue infra, not domain).

CREATE TABLE IF NOT EXISTS queue_messages (
    message_id          TEXT    PRIMARY KEY,
    queue_name          TEXT    NOT NULL,
    run_id              TEXT    NOT NULL,
    stage               TEXT    NOT NULL,
    payload             TEXT    NOT NULL,   -- JSON (JobApplicationContext.model_dump())
    attempt             INTEGER NOT NULL DEFAULT 1,
    reply_queue         TEXT    NOT NULL,
    dead_letter_queue   TEXT    NOT NULL,
    metadata            TEXT    NOT NULL,   -- JSON
    status              TEXT    NOT NULL DEFAULT 'pending',  -- pending | processing | dead
    enqueued_at         TEXT    NOT NULL,
    visible_after       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_queue_pull
    ON queue_messages(queue_name, status, visible_after);
```

The table recreation for FK removal is handled in the migration script with the
standard SQLite pattern: `CREATE TABLE new`, `INSERT INTO new SELECT FROM old`,
`DROP TABLE old`, `ALTER TABLE new RENAME TO old`.

---

## 10. What Changes vs What Stays


### Unchanged
| Component | Reason |
|-----------|--------|
| `JobApplicationContext` and all sub-models | Data contract is already correct |
| Stage business logic (`ScoringStage`, `TailoringStage`, etc.) | Pure compute, no orchestration |
| `BestFitGate.evaluate()` | Reused inside `BestFitRoutingPolicy` |
| `FormPageGate.evaluate()` | Reused inside `FormPageRoutingPolicy` |
| DB checkpoint (`upsert_checkpoint`) | Stays as `on_success` side effect in `StageWorker` |
| Artifact writing | Stays in `StageWorker._write_artifact` |

### Changed
| Component | Change |
|-----------|--------|
| `Message` | Add routing fields: `message_id`, `stage`, `reply_queue`, `dead_letter_queue` |
| `AutoRoleStage` | Replaced by `StageWorker`; `run(ctx)` → `run_forever(queue)` |
| `_run_listing` in `job_pipeline.py` | Deleted; logic distributed into workers + routing policies |
| `JobApplicationPipeline` | Retired; kept only as integration test harness calling the same queue backend |
| `inject_loop_metadata_from_gate_reason` | Moves from `pipeline.py` into `BestFitRoutingPolicy` |

### New
| Component | Notes |
|-----------|-------|
| `QueueBackend` + `SqliteQueueBackend` | Core new infrastructure |
| `RoutingPolicy`, `PassThroughPolicy`, `BestFitRoutingPolicy`, `FormPageRoutingPolicy` | Gate logic externalized |
| `WorkerConfig` | Per-worker configuration including `poll_interval_seconds` |
| `StageWorkerProcess` | Browser lifecycle management for phase 1 |
| `autorole/workers/run.py` | Entry point dispatcher |
| `autorole/workers/seed.py` | Seed script |
| Queue reaper task | Resets stuck `processing` messages |

---

## 11. File Layout

```
src/autorole/
  queue/
    __init__.py
    backend.py          # QueueBackend ABC + Message dataclass (enhanced)
    sqlite_backend.py   # SqliteQueueBackend
    reaper.py           # background visibility-timeout recovery task
  workers/
    __init__.py
    base.py             # StageWorker ABC + WorkerConfig + RoutingPolicy ABC
    policies.py         # PassThroughPolicy, BestFitRoutingPolicy, FormPageRoutingPolicy
    exploring.py        # ExploringWorker
    qualification.py    # QualificationWorker (scoring + tailoring + BestFitRoutingPolicy)
    packaging.py
    session.py
    form_intelligence.py
    form_submission.py  # FormSubmissionWorker + FormPageRoutingPolicy
    concluding.py
    process.py          # StageWorkerProcess (browser lifecycle)
    run.py              # CLI entry point
    seed.py             # seed CLI
```

Existing `stage_base.py`, `stages/`, and `gates/` remain untouched.
