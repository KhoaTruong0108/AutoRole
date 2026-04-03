# SnapFlow Handbook

## 1. Purpose

This handbook explains how to use SnapFlow in an application and how to implement or extend the framework safely.

It is split into two tracks:

1. Use SnapFlow in a project
2. Implement or extend SnapFlow internals

## 2. Mental Model

SnapFlow is an async, stage-based pipeline runtime.

Core runtime objects:

1. Context model: a run payload and metadata that moves between stages
2. Stage executor: async business logic for one stage
3. Gate: routing decision after a stage succeeds
4. Queue adapter: message transport for stage work items
5. Store adapter: persistence for context, run status, and DLQ entries
6. Runner: worker lifecycle and orchestration

Reference files:

1. [src/snapflow/core/context.py](src/snapflow/core/context.py)
2. [src/snapflow/core/result.py](src/snapflow/core/result.py)
3. [src/snapflow/core/topology.py](src/snapflow/core/topology.py)
4. [src/snapflow/runner/pipeline.py](src/snapflow/runner/pipeline.py)

## 3. Install and Import

Install local source:

```bash
python -m pip install /absolute/path/to/SnapFlow
```

Install with optional adapters and UI:

```bash
python -m pip install /absolute/path/to/SnapFlow[all]
```

Install from Git:

```bash
python -m pip install "git+https://github.com/<org>/<repo>.git"
```

Basic import:

```python
from snapflow import PipelineRunner, Topology, StageNode, Executor, Gate, StageResult, StateContext
```

## 4. Build Your First Pipeline

The simplest path is:

1. Define one or more Executor classes
2. Optionally define Gate classes for branching/loops
3. Build a Topology with stages, edges, queue backend, and store backend
4. Start a PipelineRunner and submit work

Example reference:

1. [example/demo_pipeline.py](example/demo_pipeline.py)

Minimal sketch:

```python
from snapflow import (
    Executor,
    InMemoryQueueAdapter,
    InMemoryStoreAdapter,
    PipelineRunner,
    StageNode,
    StageResult,
    StateContext,
    Topology,
)


class MyStage(Executor[dict]):
    async def execute(self, ctx: StateContext[dict]) -> StageResult[dict]:
        return StageResult.ok({**ctx.data, "done": True})


topology = Topology(
    stages=[StageNode(id="my_stage", executor=MyStage)],
    queue_backend=InMemoryQueueAdapter(),
    store_backend=InMemoryStoreAdapter(),
)

runner = PipelineRunner(topology)
```

## 5. Runtime Lifecycle

Use these runner methods:

1. start: spawn workers, optionally for selected stages only
2. run: enqueue one payload and return correlation_id
3. run_until_complete: enqueue and poll until terminal state
4. shutdown: drain or hard-stop workers

Reference:

1. [src/snapflow/runner/pipeline.py](src/snapflow/runner/pipeline.py)

Recommended pattern:

```python
await runner.start()
try:
    correlation_id = await runner.run_until_complete(data={"x": 1}, timeout=5)
finally:
    await runner.shutdown(mode="drain")
```

## 6. Data and Results

### 6.1 StateContext

`StateContext` carries:

1. correlation_id and trace_id
2. current_stage and attempt
3. typed data payload
4. artifact_refs for generated artifacts
5. metadata and timestamps

Reference:

1. [src/snapflow/core/context.py](src/snapflow/core/context.py)

### 6.2 StageResult

Return values from executors:

1. StageResult.ok(data)
2. StageResult.fail(error, error_type)

Error categories and blocked-flow semantics live in:

1. [src/snapflow/core/result.py](src/snapflow/core/result.py)

## 7. Routing, Looping, and Blocking

A Gate returns:

1. next stage id as string
2. None to complete pipeline
3. or raises BlockedError to move item to DLQ path

Reference:

1. [src/snapflow/interfaces/gate.py](src/snapflow/interfaces/gate.py)

Loop example is implemented in:

1. [example/demo_pipeline.py](example/demo_pipeline.py)

## 8. Backends and Persistence

### 8.1 Queue adapters

Available:

1. InMemoryQueueAdapter
2. SQLiteQueueAdapter
3. RedisQueueAdapter scaffold

Primary reference:

1. [src/snapflow/adapters/queues/sqlite.py](src/snapflow/adapters/queues/sqlite.py)

### 8.2 Store adapters

Available:

1. InMemoryStoreAdapter
2. SQLiteStoreAdapter
3. PostgresStoreAdapter scaffold

Primary reference:

1. [src/snapflow/adapters/stores/sqlite.py](src/snapflow/adapters/stores/sqlite.py)

### 8.3 DLQ redrive semantics

Current SQLite redrive behavior:

1. Message is requeued to stage queue
2. DLQ record is then removed from dlq_messages

This behavior is implemented through store mark_redriven in:

1. [src/snapflow/adapters/stores/sqlite.py](src/snapflow/adapters/stores/sqlite.py)

## 9. Seeder Utilities

`PipelineSeeder` helps seed runs with custom payload generation and optional CLI wrapper.

Reference:

1. [src/snapflow/seeder.py](src/snapflow/seeder.py)

Production tip:

1. Keep seed payload deterministic in tests
2. Include metadata needed for observability

## 10. CLI Operations

Entrypoint:

```bash
snapflow --help
```

Command groups:

1. queue: inspect/depth/purge
2. runs: list/show/cancel
3. dlq: list/show/redrive/purge
4. dev: worker/inject

References:

1. [src/snapflow/cli/main.py](src/snapflow/cli/main.py)
2. [src/snapflow/cli/queue_cmds.py](src/snapflow/cli/queue_cmds.py)
3. [src/snapflow/cli/run_cmds.py](src/snapflow/cli/run_cmds.py)
4. [src/snapflow/cli/dlq_cmds.py](src/snapflow/cli/dlq_cmds.py)
5. [src/snapflow/cli/dev_cmds.py](src/snapflow/cli/dev_cmds.py)

Common examples:

```bash
snapflow queue depth scoring --db example/demo_outputs/database/demo_6_stage.sqlite3
snapflow runs list --db example/demo_outputs/database/demo_6_stage.sqlite3
snapflow dlq list global_dlq --db example/demo_outputs/database/demo_6_stage.sqlite3
snapflow dlq redrive global_dlq --limit 10 --db example/demo_outputs/database/demo_6_stage.sqlite3
```

Run only selected workers from a topology module:

```bash
python -m snapflow.cli.main dev worker --topology example.demo_6_stage_pipeline:build_demo_topology --stage packaging
```

## 11. TUI Operations

TUI app factory:

1. [src/snapflow/tui/app.py](src/snapflow/tui/app.py)

Configure DB source via environment variable:

```bash
export SNAPFLOW_TUI_DB=example/demo_outputs/database/demo_6_stage.sqlite3
```

Run the TUI:

```bash
python -c "from snapflow.tui import create_tui_app; create_tui_app().run()"
```

Available tabs include Stage Monitor, Dashboard, Queue Depths, Run Inspector, and DLQ Browser.

## 12. Extension Pattern for New Projects

The recommended way to add project-specific tables and methods is subclassing the store adapter.

Reference implementation:

1. [example/project_store_adapter.py](example/project_store_adapter.py)

Pattern:

1. Extend SQLiteStoreAdapter
2. Override _ensure_initialized to create project tables
3. Add domain-specific methods such as upsert/get status
4. Use this adapter in Topology.store_backend

This keeps core framework stable and avoids editing package internals.

## 13. Implementing New Adapters

Implement these interfaces:

1. QueueAdapter in [src/snapflow/interfaces/queue.py](src/snapflow/interfaces/queue.py)
2. StoreAdapter in [src/snapflow/interfaces/store.py](src/snapflow/interfaces/store.py)

Minimum quality checklist:

1. Atomic claim/ack behavior for queues
2. Idempotent save_context and run status updates
3. DLQ listing and redrive handling
4. Graceful close and timeout behavior

## 14. Testing Strategy

Base tests already cover core and adapter behavior.

Start with:

1. [tests/test_core_models.py](tests/test_core_models.py)
2. [tests/test_topology.py](tests/test_topology.py)
3. [tests/test_runner_phase1.py](tests/test_runner_phase1.py)
4. [tests/test_sqlite_adapters.py](tests/test_sqlite_adapters.py)
5. [tests/test_tui_stage_monitor_provider.py](tests/test_tui_stage_monitor_provider.py)

Run all tests:

```bash
python -m pytest -q
```

## 15. Packaging and Distribution

Project metadata and build config are in:

1. [pyproject.toml](pyproject.toml)

Build artifacts locally:

```bash
python -m build --sdist --wheel
```

Smoke-test wheel install in a clean venv before publishing.

## 16. Troubleshooting

### 16.1 No work is being processed

1. Ensure runner.start has been called
2. Ensure stage worker set includes required stage ids
3. Check queue depth with CLI

### 16.2 Module import issues for demo topology strings

1. Use dotted module path form for topology spec
2. Example: example.demo_6_stage_pipeline:build_demo_topology

### 16.3 TUI not showing expected records

1. Verify SNAPFLOW_TUI_DB points to correct SQLite file
2. Confirm active rows exist (DLQ rows may be removed after redrive)

## 17. Where to Start Reading Code

If you are new to the codebase, read in this order:

1. [example/demo_pipeline.py](example/demo_pipeline.py)
2. [src/snapflow/core/topology.py](src/snapflow/core/topology.py)
3. [src/snapflow/runner/pipeline.py](src/snapflow/runner/pipeline.py)
4. [src/snapflow/adapters/stores/sqlite.py](src/snapflow/adapters/stores/sqlite.py)
5. [src/snapflow/cli/main.py](src/snapflow/cli/main.py)
