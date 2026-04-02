# Handoff: Per-Stage Development Harness

## Context

The event-driven worker architecture is implemented. Each stage now has a corresponding
worker under `src/autorole/workers/`. This task adds the tooling to develop and validate
each stage in isolation, without running the full pipeline.

Read these files before starting:
- `src/autorole/workers/base.py` — `StageWorker`, `WorkerConfig`, `RoutingDecision`
- `src/autorole/workers/exploring.py` — example worker with `_process` override
- `src/autorole/workers/qualification.py` — example worker with composite stage
- `src/autorole/workers/concluding.py` — example worker with `done_callback`
- `src/autorole/queue/` — `QueueBackend`, `Message`, `InMemoryQueueBackend`
- `tests/conftest.py` — existing `MockLLMClient`, `MockPage`, `MockScraper`, `SAMPLE_LISTING`
- `tests/unit/test_scoring.py` — how existing unit tests are structured (for style reference)

---

## Absolute Constraints

- Do not modify any file under `src/autorole/stages/` or `src/autorole/context.py`.
- Do not modify existing tests under `tests/unit/` or `tests/integration/`.
- The existing `MockLLMClient`, `MockPage`, `MockScraper` in `tests/conftest.py` must be
  reused — do not duplicate them. Add to `conftest.py` only if a new shared mock is needed.
- All new test files must use `pytest-asyncio`. Match the import style of existing tests.

---

## Step 1 — Make `process` public on StageWorker

**Files to modify**: `src/autorole/workers/base.py`, `src/autorole/workers/exploring.py`

In `base.py`, rename `_process` → `process` everywhere (definition and the call inside
`run_forever`).

In `exploring.py`, rename the `_process` override → `process`.

This is the primary interface for tests and the devrun CLI. Nothing else changes.

---

## Step 2 — Fixture files

**Directory**: `tests/fixtures/`

Create one JSON file per worker representing the `JobApplicationContext` state that arrives
at that worker's input queue. Each fixture is a valid `JobApplicationContext.model_dump()`
with exactly the fields populated that the stage needs as input — all other context fields
are `null`.

Use the values from `tests/conftest.py` (`SAMPLE_LISTING`, `SAMPLE_JD_HTML`) for consistency.
Timestamps must be ISO8601 strings. `run_id` should be a fixed string like `"test-run-001"`.

### Fixture population contract

| Fixture file | Fields populated (non-null) |
|---|---|
| `qualification_input.json` | `run_id`, `started_at`, `listing` |
| `packaging_input.json` | `run_id`, `started_at`, `listing`, `score`, `tailored` |
| `session_input.json` | `run_id`, `started_at`, `listing`, `score`, `tailored`, `packaged` |
| `form_intelligence_input.json` | `run_id`, `started_at`, `listing`, `score`, `tailored`, `packaged`, `session` |
| `form_submission_input.json` | `run_id`, `started_at`, `listing`, `score`, `tailored`, `packaged`, `session`, `form_intelligence`, `form_session` |
| `concluding_input.json` | all fields populated including `applied` |

`ExploringWorker` does not need a fixture — its input is a seed payload dict
(`job_url` or `search_config`), not a `JobApplicationContext`.

To construct the fixture values, look at the Pydantic model definitions in
`src/autorole/context.py` for each sub-model and use minimal but valid values.
For sub-models that contain nested Pydantic models (e.g. `FormSession.detection`),
look at their definitions in `src/autorole/integrations/form_controls/models.py`.

### Fixture helper

Add a shared fixture loader to `tests/conftest.py`:

```python
import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"

def load_fixture(name: str) -> dict:
    """Load a JSON fixture by filename from tests/fixtures/."""
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
```

Also add a shared `make_worker_message` helper to `conftest.py`:

```python
from autorole.queue import DEAD_LETTER_Q, Message

def make_worker_message(ctx_dict: dict, input_queue: str, reply_queue: str) -> Message:
    return Message(
        run_id=ctx_dict.get("run_id", "test-run-001"),
        stage=input_queue.removesuffix("_q"),
        payload=ctx_dict,
        reply_queue=reply_queue,
        dead_letter_queue=DEAD_LETTER_Q,
    )
```

---

## Step 3 — Per-worker integration tests

**Directory**: `tests/integration/`

Create one test file per worker. Each test:
1. Loads the fixture for that worker's input
2. Creates an `InMemoryQueueBackend`
3. Creates the worker with **mocked stage dependencies** (see mocking strategy below)
4. Calls `await worker.process(queue, msg)` directly
5. Asserts: output queue contents, DB state, dead letter queue is empty

### Mocking strategy

Workers that use LLM or browser are tested by mocking the underlying `*Stage` object's
`execute` method, not the worker itself. The worker's routing and persistence logic runs
for real. Only the stage's I/O is stubbed.

Create a generic `MockStage` in `tests/conftest.py`:

```python
class MockStage:
    """Stub for any stage. Returns a pre-configured StageResult on execute()."""
    def __init__(self, result: Any) -> None:
        self._result = result

    async def execute(self, message: Any) -> Any:
        _ = message
        return self._result
```

Each test builds a `StageResult`-compatible object (success=True, output=enriched_ctx_dict)
and injects it via `MockStage`. Use the next stage's fixture file as the `output` value so
the output is valid and can be validated.

### DB fixture

Add a shared async `db` fixture to `tests/conftest.py` (if not already present):

```python
import aiosqlite
import pytest_asyncio
from autorole.job_pipeline import init_db

@pytest_asyncio.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    async with aiosqlite.connect(db_path) as conn:
        await init_db(conn)
        yield conn
```

### Test structure template

```python
# tests/integration/test_<worker>_worker.py
import pytest
from autorole.context import JobApplicationContext
from autorole.queue import InMemoryQueueBackend, <INPUT_Q>, <REPLY_Q>, DEAD_LETTER_Q
from autorole.workers.<module> import <WorkerClass>
from autorole.workers.base import WorkerConfig
from tests.conftest import MockStage, load_fixture, make_worker_message

@pytest.mark.asyncio
async def test_<worker>_success(db, tmp_path):
    input_fixture  = load_fixture("<worker>_input.json")
    output_fixture = load_fixture("<next_worker>_input.json")   # expected enriched output

    # Arrange
    queue   = InMemoryQueueBackend()
    config  = WorkerConfig(input_queue=<INPUT_Q>, reply_queue=<REPLY_Q>,
                           dead_letter_queue=DEAD_LETTER_Q, poll_interval_seconds=0)
    stage   = MockStage(result=_make_result(success=True, output=output_fixture))
    worker  = <WorkerClass>(stage=stage, repo=..., logger=...,
                             artifacts_root=tmp_path, config=config)
    msg     = make_worker_message(input_fixture, <INPUT_Q>, <REPLY_Q>)

    # Act
    await worker.process(queue, msg)

    # Assert — output queue
    out = await queue.pull(<REPLY_Q>)
    assert out is not None
    result_ctx = JobApplicationContext.model_validate(out.payload)
    assert result_ctx.<expected_field> is not None

    # Assert — checkpoint written
    checkpoint = await repo.get_checkpoint(input_fixture["run_id"])
    assert checkpoint is not None
    assert checkpoint[0] == "<worker_name>"

    # Assert — dead letter is empty
    assert await queue.pull(DEAD_LETTER_Q) is None


@pytest.mark.asyncio
async def test_<worker>_stage_failure_routes_to_dlq(db, tmp_path):
    # Same setup but MockStage returns success=False
    # Assert: dead_letter_q has one message, reply_queue is empty
    ...


@pytest.mark.asyncio
async def test_<worker>_unhandled_exception_nacks(db, tmp_path):
    # MockStage raises an exception
    # Assert: input_queue still has the message (nacked), reply and dlq empty
    ...
```

### Per-worker test notes

**`ExploringWorker`** (`test_exploring_worker.py`):
- Input msg payload is `{"job_url": "https://example.com/job/123", "max_listings": 1}`.
- `MockStage.execute` returns a list of one `JobApplicationContext` (use `qualification_input.json`).
- Assert: `SCORING_Q` has one message, `upsert_listing` persisted to DB (query `job_listings`).
- Fanout test: `max_listings=2`, MockStage returns two contexts — assert two messages in `SCORING_Q`.

**`QualificationWorker`** (`test_qualification_worker.py`):
- `QualificationWorker` wraps a composite `_QualificationStage` internally.
- Inject mock via two separate `MockStage` instances — one for scoring, one for tailoring.
  Construct: `QualificationWorker(scoring_stage=mock_scoring, tailoring_stage=mock_tailoring, ...)`.
- Loop test: first call returns `tailoring_degree=1` (triggers BestFitGate loop), second returns
  `tailoring_degree=0` (gate passes). Assert `PACKAGING_Q` gets message on second call.
- Block test: `max_attempts=1`, gate blocks after first attempt. Assert `DEAD_LETTER_Q`.

**`FormSubmissionWorker`** (`test_form_submission_worker.py`):
- Loop test: `FormPageRoutingPolicy` says loop → assert message re-enqueued to `FORM_INTEL_Q`,
  not `FORM_SUB_Q`. This is the one structural special case.
- Pass test: gate says pass → assert message in `CONCLUDING_Q`.

**`ConcludingWorker`** (`test_concluding_worker.py`):
- Assert `done_callback` is called on success.
- Assert `upsert_application` persisted (query `job_applications` table).
- No reply queue assertion needed (concluding is terminal — it enqueues to its own reply_queue
  which is `CONCLUDING_Q` by topology; the message is just acked).

---

## Step 4 — devrun CLI

**File**: `src/autorole/workers/devrun.py`

Interactive development tool. Runs exactly one `worker.process(queue, msg)` with real
dependencies (real LLM, real Playwright, real renderer) then exits and reports results.

### CLI interface

```
python -m autorole.workers.devrun --stage <name> --input-run-id <run_id>
python -m autorole.workers.devrun --stage <name> --input-file <path/to/fixture.json>
python -m autorole.workers.devrun --stage <name> --input-file <path> --dry-run
```

- `--stage`: one of `exploring`, `qualification`, `packaging`, `session`,
  `form_intelligence`, `form_submission`, `concluding`
- `--input-run-id`: load ctx from DB checkpoint for this run_id; seed the worker's input queue
- `--input-file`: load ctx from a JSON file (fixture or exported checkpoint)
- `--dry-run`: print what would be enqueued without actually running the stage

### Behaviour

1. Load `AppConfig` from default location.
2. If `--input-run-id`: connect to DB, call `repo.get_checkpoint(run_id)`, use `context_json` as payload.
3. If `--input-file`: read and parse JSON directly.
4. Build the target worker with real dependencies using the same construction logic as
   `JobApplicationPipeline._build_workers`. Use `InMemoryQueueBackend` (not SQLite) so
   the devrun doesn't pollute the production queue.
5. Build a `Message` with `reply_queue` and `dead_letter_queue` set to their standard queue names.
6. Call `await worker.process(queue, msg)`.
7. After processing, print a structured report:

```
=== devrun: <stage> ===
run_id:   <run_id>
decision: pass | loop | block

[output queue: <reply_queue>]
  message_id: <id>
  payload keys: run_id, listing, score, tailored, ...

[dead_letter_q]
  empty

[artifacts]
  <artifacts_root>/<run_id>/<stage>/...

[db checkpoint]
  last_success_stage: <stage>
```

For browser-dependent stages (`qualification`, `session`, `form_intelligence`,
`form_submission`), launch Playwright via `async_playwright` inside `devrun`, create a
single `BrowserContext`, and pass the appropriate `Page` to the stage constructor.
Use `headless=False` by default (visible browser for dev inspection); add `--headless` flag.

### Observe mode

Add `--mode observe` flag (default `observe`). When observe mode is active, skip
`session`, `form_intelligence`, `form_submission` workers — print a warning instead.
This matches the existing `job_pipeline.py` observe mode semantics.

---

## Files to Create

```
tests/fixtures/qualification_input.json
tests/fixtures/packaging_input.json
tests/fixtures/session_input.json
tests/fixtures/form_intelligence_input.json
tests/fixtures/form_submission_input.json
tests/fixtures/concluding_input.json
tests/integration/test_exploring_worker.py
tests/integration/test_qualification_worker.py
tests/integration/test_packaging_worker.py
tests/integration/test_session_worker.py
tests/integration/test_form_intelligence_worker.py
tests/integration/test_form_submission_worker.py
tests/integration/test_concluding_worker.py
src/autorole/workers/devrun.py
```

## Files to Modify

```
src/autorole/workers/base.py       — rename _process → process
src/autorole/workers/exploring.py  — rename _process override → process
tests/conftest.py                  — add MockStage, load_fixture, make_worker_message,
                                     db fixture (if not present)
```

## Files to Leave Untouched

```
src/autorole/stages/*
src/autorole/context.py
src/autorole/workers/base.py       (except the rename)
src/autorole/workers/*.py          (except exploring.py rename)
tests/unit/*
tests/integration/test_pipeline_e2e.py
```

---

## Checklist Before Marking Done

- [ ] `StageWorker.process` is public in `base.py`; `run_forever` calls `self.process`
- [ ] `ExploringWorker.process` override is public
- [ ] `load_fixture` and `make_worker_message` are in `tests/conftest.py`
- [ ] `MockStage` is in `tests/conftest.py`
- [ ] `db` async fixture is in `tests/conftest.py`
- [ ] All 6 fixture JSON files exist and are valid `JobApplicationContext.model_dump()` payloads
- [ ] Each fixture file has exactly the fields populated per the contract table in Step 2
- [ ] All 7 worker integration test files exist with success + failure + exception test cases
- [ ] `FormSubmissionWorker` loop test asserts re-enqueue to `FORM_INTEL_Q` not `FORM_SUB_Q`
- [ ] `ExploringWorker` fanout test asserts N messages in `SCORING_Q`
- [ ] `QualificationWorker` loop test covers BestFitGate loop → pass path
- [ ] `ConcludingWorker` test asserts `done_callback` is called
- [ ] `devrun.py` accepts `--stage`, `--input-run-id`, `--input-file`, `--headless`, `--mode`
- [ ] `devrun.py` prints structured report after `worker.process` completes
- [ ] All existing tests in `tests/unit/` and `tests/integration/test_pipeline_e2e.py` still pass
