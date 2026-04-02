# Handoff: Implement Event-Driven Stage Workers

## Context

AutoRole is a job application automation tool. The current `src/autorole/job_pipeline.py`
wires all stages into a monolithic in-process orchestrator. The goal of this task is to
refactor it into an event-driven, queue-backed worker architecture where each stage runs
independently: pull message → process → route to next queue on success, nack/DLQ on failure.

Read the full LLD before starting: `docs/lld-event-driven-workers.md`.

Before writing any code, read these files in full to understand what already exists:

- `src/autorole/job_pipeline.py` — current orchestrator (will be refactored)
- `src/autorole/stage_base.py` — `AutoRoleStage` (will be replaced by `StageWorker`)
- `src/autorole/context.py` — `JobApplicationContext` and all sub-models (DO NOT MODIFY)
- `src/autorole/pipeline.py` — `Message`, `GateDecision`, `GateResult`, `inject_loop_metadata_from_gate_reason`
- `src/autorole/gates/best_fit.py` — `BestFitGate`
- `src/autorole/gates/form_page.py` — `FormPageGate`
- `src/autorole/db/repository.py` — `JobRepository`
- `src/autorole/db/migrations/001_domain.sql` — existing schema
- `src/autorole/stages/` — all stage implementations (DO NOT MODIFY)

---

## Absolute Constraints

- **Do not modify** any file under `src/autorole/stages/` or `src/autorole/context.py`.
- **Do not modify** `BestFitGate.evaluate()` or `FormPageGate.evaluate()` — wrap them, don't change them.
- **Do not modify** `JobRepository` methods — add new ones if needed, don't change existing signatures.
- All new code must be `async`-first (the project uses `asyncio` throughout).
- Use `aiosqlite` for all SQLite access (already a dependency).
- Use Python dataclasses or Pydantic for data models — match the style already in the file you're working near.
- Type-annotate everything. The project uses `from __future__ import annotations` at the top of every file.

---

## Implementation Order

Follow this order strictly. Each step depends on the previous.

### Step 1 — Enhanced Message + QueueBackend interface

**File**: `src/autorole/queue/backend.py`

Replace the existing `Message` dataclass (currently defined in both `job_pipeline.py` and `pipeline.py`)
with an enhanced version. The new `Message` is the single source of truth — import it everywhere.

```python
@dataclass
class Message:
    run_id: str
    stage: str
    payload: dict[str, Any]
    reply_queue: str
    dead_letter_queue: str
    message_id: str = field(default_factory=lambda: str(uuid4()))
    attempt: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
```

Define the `QueueBackend` ABC in the same file:

```python
class QueueBackend(ABC):
    @abstractmethod
    async def enqueue(self, queue_name: str, message: Message) -> str: ...

    @abstractmethod
    async def pull(self, queue_name: str, visibility_timeout_seconds: int = 300) -> Message | None: ...

    @abstractmethod
    async def ack(self, queue_name: str, message_id: str) -> None: ...

    @abstractmethod
    async def nack(self, queue_name: str, message_id: str, delay_seconds: int = 0) -> None: ...

    @abstractmethod
    async def create_queue(self, queue_name: str) -> None: ...
```

Also define queue name constants in this file:

```python
EXPLORING_Q      = "exploring_q"
SCORING_Q        = "scoring_q"
PACKAGING_Q      = "packaging_q"
SESSION_Q        = "session_q"
FORM_INTEL_Q     = "form_intel_q"
FORM_SUB_Q       = "form_sub_q"
CONCLUDING_Q     = "concluding_q"
DEAD_LETTER_Q    = "dead_letter_q"
```

---

### Step 2 — SQLite Queue Backend

**File**: `src/autorole/queue/sqlite_backend.py`

Implement `SqliteQueueBackend(QueueBackend)`. It receives an `aiosqlite.Connection` at construction.

Key behaviours:

- `enqueue`: INSERT into `queue_messages` with `status='pending'`, `visible_after=now`, `enqueued_at=now`. Return `message_id`.
- `pull`: Atomically claim one `pending` message where `visible_after <= now`. Use a transaction:
  SELECT the first eligible row, UPDATE its `status='processing'` and `visible_after=now+timeout`, return it.
  Return `None` if no rows match. Serialize/deserialize `payload` and `metadata` as JSON.
- `ack`: DELETE the row by `message_id`.
- `nack`: UPDATE `status='pending'`, `visible_after=now+delay_seconds` for the given `message_id`.
- `create_queue`: No-op (queue_messages is a single table; queue_name is just a column value).

The `pull` must be atomic to prevent double-claiming when multiple workers share the same SQLite file.
Use `BEGIN IMMEDIATE` or equivalent to achieve this.

---

### Step 3 — In-Memory Queue Backend

**File**: `src/autorole/queue/memory_backend.py`

Implement `InMemoryQueueBackend(QueueBackend)` for use in the e2e test harness.

Uses `dict[str, asyncio.Queue[Message]]` internally, keyed by queue name.

- `enqueue`: `queue.put_nowait(message)`. Auto-create queue if not present. Return `message.message_id`.
- `pull`: `queue.get_nowait()`. Return `None` on `asyncio.QueueEmpty`. Ignore `visibility_timeout_seconds`.
- `ack`: No-op (message already consumed by `get_nowait()`).
- `nack`: `queue.put_nowait(message)`. Ignore `delay_seconds`.
- `create_queue`: Pre-create the `asyncio.Queue` for this name if not present.

The `InMemoryQueueBackend` also needs to store the original `Message` object for `nack` to re-enqueue it.
Since `pull` already returns the message, `nack` receives it as a parameter — but `nack`'s signature
only has `message_id`. Store a `dict[str, Message]` (`_in_flight`) populated on `pull`, cleared on `ack`/`nack`.

---

### Step 4 — Queue Reaper

**File**: `src/autorole/queue/reaper.py`

`async def run_reaper(db: aiosqlite.Connection, interval_seconds: float = 30.0) -> None`

Runs forever. Every `interval_seconds`, executes:

```sql
UPDATE queue_messages
SET status = 'pending', visible_after = datetime('now')
WHERE status = 'processing'
  AND visible_after < datetime('now')
```

This recovers messages stuck in `processing` due to worker crashes. Use `asyncio.sleep(interval_seconds)`
between iterations. Designed to run as a background task alongside workers.

---

### Step 5 — DB Migration 002

**File**: `src/autorole/db/migrations/002_queue.sql`

Two parts:

**Part A** — Create `queue_messages` table (no FK on `run_id`):

```sql
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
```

**Part B** — Remove `REFERENCES job_listings(run_id)` FK declarations from:
`score_reports`, `tailored_resumes`, `session_records`, `job_applications`, `pipeline_checkpoints`.

SQLite does not support `DROP CONSTRAINT`. Use the standard recreate pattern for each table:
1. `CREATE TABLE <name>_new ( ... )` — same columns, same constraints, **without** `REFERENCES` on `run_id`
2. `INSERT INTO <name>_new SELECT * FROM <name>`
3. `DROP TABLE <name>`
4. `ALTER TABLE <name>_new RENAME TO <name>`
5. Recreate any indexes that were on the original table

Do this for all five affected tables. Preserve every column, default value, and non-FK constraint exactly.

Also update `src/autorole/job_pipeline.py`'s `init_db` function to run both `001_domain.sql`
and `002_queue.sql` migrations.

---

### Step 6 — RoutingPolicy + WorkerConfig

**File**: `src/autorole/workers/base.py`

Define `WorkerConfig` as a dataclass:

```python
@dataclass
class WorkerConfig:
    input_queue: str
    reply_queue: str
    dead_letter_queue: str
    poll_interval_seconds: float = 2.0
    visibility_timeout_seconds: int = 300
    max_attempts: int = 3
```

Define the routing contracts:

```python
@dataclass
class RoutingDecision:
    decision: Literal["pass", "loop", "block"]
    reason: str = ""

class RoutingPolicy(ABC):
    @abstractmethod
    def evaluate(self, result: Any, message: Message) -> RoutingDecision: ...
```

---

### Step 7 — Routing Policies

**File**: `src/autorole/workers/policies.py`

Implement three policies:

**`PassThroughPolicy(RoutingPolicy)`**:
- Returns `pass` if `result.success` is True, `block` otherwise.
- `reason` is `result.error` on block, empty string on pass.

**`BestFitRoutingPolicy(RoutingPolicy)`**:
- Construct with `gate: BestFitGate`.
- `evaluate(result, message)`:
  1. If not `result.success`: return `block` with `result.error`.
  2. Call `self._gate.evaluate(result, message)` — this returns a `GateResult`.
  3. Map `GateDecision.PASS` → `RoutingDecision("pass")`.
  4. Map `GateDecision.BLOCK` → `RoutingDecision("block", reason)`.
  5. Map `GateDecision.LOOP` → `RoutingDecision("loop", reason)`.
- Move `inject_loop_metadata_from_gate_reason` from `pipeline.py` into this file as a
  private helper `_inject_loop_metadata(metadata, reason) -> dict`. Call it inside `evaluate`
  before returning a `loop` decision, updating `message.metadata` in the returned decision.
  Attach the updated metadata to the `RoutingDecision` so `StageWorker._build_loop_message`
  can use it. Add a `metadata: dict` field to `RoutingDecision` for this purpose.

**`FormPageRoutingPolicy(RoutingPolicy)`**:
- Construct with `gate: FormPageGate`.
- Same mapping pattern as `BestFitRoutingPolicy`.
- On `loop`, the loop target is `form_intel_q` — but routing policy does not hardcode queue names.
  Instead, the `StageWorker` for `FormSubmissionWorker` overrides the loop enqueue target.
  Return `RoutingDecision("loop", reason)` as normal; the worker handles the special queue.

---

### Step 8 — StageWorker base class

**File**: `src/autorole/workers/base.py` (extend the file from Step 6)

```python
class StageWorker(ABC):
    name: str

    def __init__(
        self,
        stage: Any,
        repo: JobRepository,
        logger: logging.Logger,
        artifacts_root: Path,
        config: WorkerConfig,
        routing_policy: RoutingPolicy | None = None,
    ) -> None: ...
```

Implement these methods:

**`async run_forever(queue: QueueBackend) -> None`**:
```
await queue.create_queue(config.input_queue)
while True:
    msg = await queue.pull(config.input_queue, config.visibility_timeout_seconds)
    if msg is None:
        await asyncio.sleep(config.poll_interval_seconds)
        continue
    await self._process(queue, msg)
```

**`async _process(queue: QueueBackend, msg: Message) -> None`**:
```
result = await self._execute_inner(msg)

if result is None:
    delay = self._backoff(msg.attempt)
    await queue.nack(config.input_queue, msg.message_id, delay)
    logger.exception("unhandled exception stage=%s run_id=%s", self.name, msg.run_id)
    return

policy = self._routing_policy or PassThroughPolicy()
decision = policy.evaluate(result, msg)

match decision.decision:
    case "pass":
        enriched = self._enrich(msg, result.output)
        await queue.enqueue(msg.reply_queue, enriched)
        await queue.ack(config.input_queue, msg.message_id)
        ctx = JobApplicationContext.model_validate(result.output)
        await self.on_success(ctx, msg.attempt)
        await self._repo.upsert_checkpoint(ctx.run_id, self.name, ctx.model_dump(mode="json"))
        self.log_ok(ctx, msg.attempt)

    case "loop":
        loop_msg = self._build_loop_message(msg, decision)
        await queue.enqueue(self._loop_queue(msg), loop_msg)
        await queue.ack(config.input_queue, msg.message_id)

    case "block":
        await queue.enqueue(msg.dead_letter_queue, msg)
        await queue.ack(config.input_queue, msg.message_id)
        logger.warning("blocked stage=%s run_id=%s reason=%s", self.name, msg.run_id, decision.reason)
```

**`_loop_queue(msg: Message) -> str`**: Returns `config.input_queue` by default.
`FormSubmissionWorker` overrides this to return `FORM_INTEL_Q`.

**`_enrich(msg: Message, output: dict) -> Message`**:
Return a new `Message` with `payload=output`, `attempt=1`, same `run_id`, `reply_queue`,
`dead_letter_queue`, `stage=next_stage_name`. Generate a new `message_id`.

**`_build_loop_message(msg: Message, decision: RoutingDecision) -> Message`**:
Return a new `Message` with `attempt=msg.attempt+1`, `metadata=decision.metadata` (updated by policy),
same `payload`, `reply_queue`, `dead_letter_queue`. Generate a new `message_id`.

**`_backoff(attempt: int) -> int`**: `return min(2 ** attempt, 60)`

**`async _execute_inner(msg: Message) -> StageResult | None`**:
```python
try:
    return await self._stage.execute(msg)
except Exception:
    return None
```

**`def _write_artifact(filename, content, run_id) -> Path`** and
**`def _append_stage_index(run_dir, filename)`**:
Copy these verbatim from `AutoRoleStage` in `stage_base.py`. Logic is identical.

**Abstract methods** (subclasses must implement):
- `async on_success(ctx: JobApplicationContext, attempt: int) -> None`
- `def log_ok(ctx: JobApplicationContext, attempt: int) -> None`

---

### Step 9 — Individual Workers

Create one file per worker under `src/autorole/workers/`. Each worker:
1. Extends `StageWorker`
2. Sets `name: str` as a class variable
3. Implements `on_success` (domain DB writes) and `log_ok` (print statement)
4. Is constructed with the appropriate stage, config, and routing policy

Reference the existing `*Executor` classes in the stage files for what `on_success` should write.
The `*Executor` classes in `src/autorole/stages/` are the direct source of truth for what DB
writes belong to each stage.

**`src/autorole/workers/exploring.py`** — `ExploringWorker`:
- `name = "exploring"`
- Overrides `_process` entirely (fanout pattern — see LLD §4).
  On success: for each `ctx` in `result.output`, call `upsert_listing(ctx.listing, ctx.run_id)`,
  then enqueue a new child `Message` into `config.reply_queue` (which is `SCORING_Q`).
  Ack the original message after all children are enqueued.
  On failure: nack with backoff (same as base).
- `on_success`: no-op (handled inline in overridden `_process`).

**`src/autorole/workers/qualification.py`** — `QualificationWorker`:
- `name = "qualification"`
- Constructed with both `ScoringStage` and `TailoringStage`. Runs scoring then tailoring inline.
- Routing policy: `BestFitRoutingPolicy(BestFitGate(max_attempts=config.tailoring.max_attempts))`.
- `on_success`: `upsert_score(run_id, ctx.score, attempt)` + `upsert_tailored(run_id, ctx.tailored)`.

**`src/autorole/workers/packaging.py`** — `PackagingWorker`:
- `name = "packaging"`
- Routing policy: `PassThroughPolicy`.
- `on_success`: write packaging artifact (PDF path). No specific `JobRepository` method needed beyond checkpoint.

**`src/autorole/workers/session.py`** — `SessionWorker`:
- `name = "session"`
- Routing policy: `PassThroughPolicy`.
- `on_success`: `upsert_session(run_id, ctx.session)`.

**`src/autorole/workers/form_intelligence.py`** — `FormIntelligenceWorker`:
- `name = "form_intelligence"`
- Routing policy: `PassThroughPolicy`.
- `on_success`: write form intelligence artifact. No specific repo method beyond checkpoint.

**`src/autorole/workers/form_submission.py`** — `FormSubmissionWorker`:
- `name = "form_submission"`
- Routing policy: `FormPageRoutingPolicy(FormPageGate())`.
- Overrides `_loop_queue` to return `FORM_INTEL_Q`.
- `on_success`: write submission artifact. No specific repo method beyond checkpoint.

**`src/autorole/workers/concluding.py`** — `ConcludingWorker`:
- `name = "concluding"`
- Routing policy: `PassThroughPolicy`.
- Constructor accepts an optional `done_callback: Callable[[], None] | None = None`.
- `on_success`: `upsert_application(...)` + call `done_callback()` if not None.

---

### Step 10 — Refactor job_pipeline.py

`JobApplicationPipeline` becomes the e2e test harness. Preserve `RunConfig` unchanged.

Replace `_build_executors` with `_build_workers` that returns `dict[str, StageWorker]`, using
`InMemoryQueueBackend` and `WorkerConfig(poll_interval_seconds=0, ...)` for all workers.

Replace `_run_listing` and the main `run()` flow with:

```python
async def run(self) -> int:
    queue = InMemoryQueueBackend()
    done_event = asyncio.Event()

    # browser + playwright setup (unchanged from current)
    ...

    workers = self._build_workers(repo, queue, done_event, ...)

    if is_resume_mode:
        # load checkpoint, seed appropriate stage queue
        checkpoint = await repo.get_checkpoint(resume_run_id)
        last_stage, checkpoint_ctx = checkpoint
        resume_ctx = JobApplicationContext.model_validate(checkpoint_ctx)
        start_queue = _stage_to_queue(rc.from_stage or _next_stage(last_stage))
        seed_msg = _make_seed_message(resume_ctx.run_id, resume_ctx.model_dump(), start_queue)
        await queue.enqueue(start_queue, seed_msg)
    else:
        seed_msg = _make_seed_message("seed", seed_payload, EXPLORING_Q)
        await queue.enqueue(EXPLORING_Q, seed_msg)

    tasks = [asyncio.create_task(w.run_forever(queue)) for w in workers.values()]

    try:
        await asyncio.wait_for(done_event.wait(), timeout=3600.0)
    except asyncio.TimeoutError:
        trace_logger.error("Pipeline timed out")
        return 1
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    return 0
```

Add a helper `_stage_to_queue(stage_name: str) -> str` that maps stage names to queue constants.
Add a helper `_make_seed_message(run_id, payload, target_queue) -> Message`.

Remove: `_run_listing`, `_execute_stage`, `should_run`, `inject_loop_metadata_from_gate_reason` usage.

Keep: `RunConfig`, `make_llm_client`, `make_renderer`, `init_db`, `_configure_trace_logger`.

Update `init_db` to also run `002_queue.sql`.

---

### Step 11 — Entry Points

**`src/autorole/workers/run.py`**:

```python
# python -m autorole.workers.run --stage <name>
# Starts a single worker process with SqliteQueueBackend.
# Handles browser lifecycle via StageWorkerProcess for browser-dependent stages.
```

**`src/autorole/workers/seed.py`**:

```python
# python -m autorole.workers.seed --job-url <url>
# python -m autorole.workers.seed --search
# python -m autorole.workers.seed --resume-run-id <id> [--from-stage <stage>]
# Constructs a seed Message and enqueues it into the appropriate queue, then exits.
```

`StageWorkerProcess` in `src/autorole/workers/process.py` manages the browser context lifecycle
for workers that need Playwright. It accepts a list of `StageWorker` instances and a `QueueBackend`,
launches a shared `BrowserContext`, assigns each worker its own `Page`, then runs all workers
via `asyncio.gather`.

---

## Checklist Before Marking Done

- [ ] `src/autorole/queue/__init__.py` exports `QueueBackend`, `Message`, queue name constants
- [ ] `src/autorole/workers/__init__.py` exports all worker classes
- [ ] `InMemoryQueueBackend.nack` correctly re-enqueues (uses `_in_flight` dict)
- [ ] `SqliteQueueBackend.pull` is atomic (no double-claim under concurrent access)
- [ ] `002_queue.sql` fully recreates all five affected domain tables without `REFERENCES` clauses
- [ ] `init_db` in `job_pipeline.py` runs both migrations
- [ ] `BestFitRoutingPolicy` correctly calls `inject_loop_metadata` and attaches updated metadata to `RoutingDecision`
- [ ] `FormSubmissionWorker._loop_queue` returns `FORM_INTEL_Q`, not `FORM_SUB_Q`
- [ ] `ExploringWorker` calls `upsert_listing` for each ctx before enqueuing to `scoring_q`
- [ ] `ConcludingWorker` calls `done_callback()` if not None
- [ ] `job_pipeline.py` uses `InMemoryQueueBackend` and `poll_interval_seconds=0`
- [ ] `job_pipeline.py` resume mode seeds the correct queue (not always `exploring_q`)
- [ ] No files under `src/autorole/stages/` are modified
- [ ] `src/autorole/context.py` is not modified
- [ ] Existing tests in `tests/` still pass without modification

## Files to Create

```
src/autorole/queue/__init__.py
src/autorole/queue/backend.py
src/autorole/queue/sqlite_backend.py
src/autorole/queue/memory_backend.py
src/autorole/queue/reaper.py
src/autorole/workers/__init__.py
src/autorole/workers/base.py
src/autorole/workers/policies.py
src/autorole/workers/exploring.py
src/autorole/workers/qualification.py
src/autorole/workers/packaging.py
src/autorole/workers/session.py
src/autorole/workers/form_intelligence.py
src/autorole/workers/form_submission.py
src/autorole/workers/concluding.py
src/autorole/workers/process.py
src/autorole/workers/run.py
src/autorole/workers/seed.py
src/autorole/db/migrations/002_queue.sql
```

## Files to Modify

```
src/autorole/job_pipeline.py   — refactor to e2e harness (see Step 10)
```

## Files to Leave Untouched

```
src/autorole/context.py
src/autorole/stage_base.py     — kept for reference; superseded by StageWorker but not deleted
src/autorole/pipeline.py       — kept; inject_loop_metadata_from_gate_reason stays as deprecated shim
src/autorole/stages/*
src/autorole/gates/*
src/autorole/db/repository.py
src/autorole/db/migrations/001_domain.sql
tests/*
```
