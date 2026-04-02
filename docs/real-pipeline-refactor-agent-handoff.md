# Real Pipeline Runner Refactor - Agent Handoff Plan

## Goal
Simplify [scripts/run_real_pipeline.py](scripts/run_real_pipeline.py) by extracting repeated stateful behavior into a common parent/base execution layer while preserving existing runtime behavior.

Primary outcomes:
1. Keep business flow unchanged.
2. Centralize common stage state behavior:
- artifact persistence
- DB checkpoint persistence
- structured error logging
- resume hint generation
- stage retry/resume from prior run when inputs are satisfied
3. Reduce complexity of `run_listing` and `amain`.

## Current Responsibilities in scripts/run_real_pipeline.py
The script currently handles all of the following in one module:
1. CLI parsing and mode semantics (`observe`, `apply`, `apply-dryrun`).
2. Runtime environment setup (paths, logs, db init).
3. External dependency construction (browser, scrapers, llm, renderer, stage objects).
4. Main workflow orchestration for every stage.
5. Scoring-tailoring retry loop and gate decision handling.
6. Stage-level fallback behavior for dry-run.
7. Artifact writing and stage output indexing.
8. Stage checkpoint persistence.
9. Resume command generation and error hints.
10. Resume-mode checkpoint loading and stage-start inference.

This concentration is the main complexity source.

## Core Refactor Principle
Move repeated state behavior to a shared base class, then keep stage-specific payload formatting in thin adapters.

## Proposed Target Structure

### 1) New module: src/autorole/runner/stateful_execution.py
Create these abstractions:

#### A. StageStateManager (common parent/base behavior)
Responsibilities:
1. Write artifacts.
2. Maintain `stage_outputs.md` index.
3. Save checkpoints after stage success.
4. Save standardized error artifacts.
5. Emit standardized error logs.
6. Emit resume command hints.

Proposed API:
1. `record_json(stage_name, filename, obj)`
2. `record_text(stage_name, filename, content)`
3. `record_error(stage_name, error_type, error, extra=None)`
4. `checkpoint(stage_name, ctx)`
5. `fail(stage_name, error_type, error, mode, resume_stage)`
6. `ok(stage_name, summary_line=None)`

#### B. StageExecutor (common stage call wrapper)
Responsibilities:
1. Execute stage with uniform try/except.
2. Return normalized result shape.
3. Keep all stages consistent with the same failure handling contract.

Proposed API:
1. `execute(stage_name, stage, message) -> StageExecResult`

`StageExecResult` fields:
1. `transport_ok: bool` (stage call did not crash)
2. `stage_ok: bool` (`result.success` true)
3. `result: Any | None`
4. `error_type: str`
5. `error: str`

#### C. ResumePolicy (resume/retry contract)
Responsibilities:
1. Determine whether a stage can be resumed from checkpoint.
2. Validate required inputs for target stage.
3. Return actionable failure reason when preconditions are missing.

Proposed API:
1. `can_resume(stage_name, ctx) -> tuple[bool, str]`
2. `required_fields(stage_name) -> list[str]`

Initial required fields map:
1. `exploring`: `listing` for resume mode skip logic
2. `scoring`: `listing`
3. `tailoring`: `listing`, `score`
4. `packaging`: `listing`, `tailored`
5. `session`: `listing`
6. `form_intelligence`: `listing`, `packaged`
7. `form_submission`: `listing`, `form_intelligence`, `packaged`
8. `concluding`: `listing`, `score`, `tailored`, `packaged`, `applied`

This makes "resume & retry at any stage from previous run as long as the inputs are satisfied" explicit and enforceable.

### 2) Keep scripts/run_real_pipeline.py as orchestrator shell
After extraction, this file should mostly do:
1. Parse args.
2. Build runtime dependencies.
3. Select contexts (manual/resume/explore path).
4. Delegate stage execution to a coordinator that uses StageStateManager + StageExecutor.

## Proposed Execution Coordinator

### New module: src/autorole/runner/coordinator.py
Create `ListingCoordinator`.

Responsibilities:
1. Execute linear stages in order.
2. Execute scoring-tailoring loop.
3. Delegate all common state operations to StageStateManager.
4. Delegate stage calls to StageExecutor.
5. Delegate precondition validation to ResumePolicy.

Methods:
1. `run_listing(ctx, mode, start_stage)`
2. `_run_scoring_tailoring_loop(ctx, mode)`
3. `_run_single_stage(stage_name, ctx, mode)`

## Phased Refactor Plan

### Phase 1 (Low Risk, behavior-preserving extraction)
1. Introduce `StageStateManager` in new module.
2. Move artifact write/index/update helpers from script into manager.
3. Move standardized error artifact + resume hint formatting into manager.
4. Keep orchestration in script but call manager APIs.

Exit criteria:
1. Same artifact filenames and folder layout.
2. Same checkpoint writes.
3. Same error logs and resume command outputs.

### Phase 2 (Low-Medium Risk)
1. Add `StageExecutor` and replace repeated `_execute_stage + success/failure checks` blocks.
2. Keep stage-specific artifact content generation where it is.
3. Centralize print/log message formats.

Exit criteria:
1. No stage-specific logic regressions.
2. Same failure behavior for all stages.

### Phase 3 (Medium Risk)
1. Extract scoring-tailoring loop into coordinator method.
2. Preserve gate handling and metadata injection behavior exactly.
3. Keep same attempt increment semantics.

Exit criteria:
1. Loop/pass/block outcomes unchanged for existing scenarios.

### Phase 4 (Medium-High Risk)
1. Add `ResumePolicy` and enforce precondition checks before resume or stage jump.
2. In resume mode, if preconditions fail for requested `from_stage`, stop with clear actionable message.
3. Add helper for deriving `start_stage` from checkpoint and explicit override.

Exit criteria:
1. Resume works from any stage where context has required fields.
2. Resume fails fast and clearly when required fields are missing.

### Phase 5 (Optional final cleanup)
1. Move dependency factory logic to `src/autorole/runner/factories.py`.
2. Keep script as thin entrypoint only.

## Required Invariants (Do Not Change)
1. Existing stage order and mode semantics.
2. Existing checkpoint table behavior.
3. Existing artifact naming conventions.
4. Existing dry-run behavior currently implemented.
5. Existing gate decision semantics in scoring-tailoring loop.

## New Tests To Add

### Unit tests for common/base classes
1. StageStateManager writes JSON/text/error artifacts and updates index.
2. StageStateManager checkpoint helper writes expected checkpoint state.
3. StageExecutor returns normalized transport/stage result for:
- stage crash
- stage fail
- stage success
4. ResumePolicy precondition checks for each stage.

### Integration tests for coordinator
1. Resume from each stage with sufficient context succeeds.
2. Resume from each stage with missing inputs fails with explicit missing fields.
3. Loop path remains identical (loop, block, pass).
4. Artifact parity test for one end-to-end run.

## Implementation Notes for Next Agent
1. Keep changes behavior-preserving in phases 1-3.
2. Use strict small commits per phase.
3. After each phase run:
- targeted unit tests
- one manual `apply-dryrun` command
- one resume command
4. Avoid changing stage internals during runner refactor.
5. Avoid changing DB schema unless absolutely required.

## Suggested Commit Sequence
1. `refactor(runner): extract state manager for artifacts/checkpoints/errors`
2. `refactor(runner): add stage executor normalization`
3. `refactor(runner): extract scoring-tailoring coordinator loop`
4. `feat(runner): add resume precondition policy for stage-level resume`
5. `refactor(runner): thin script entrypoint and move factories`

## Primary Handoff File Targets
1. [scripts/run_real_pipeline.py](scripts/run_real_pipeline.py)
2. [src/autorole/runner/stateful_execution.py](src/autorole/runner/stateful_execution.py)
3. [src/autorole/runner/coordinator.py](src/autorole/runner/coordinator.py)
4. [src/autorole/runner/factories.py](src/autorole/runner/factories.py)
5. New tests under [tests/unit](tests/unit) and [tests/integration](tests/integration)

## Success Definition
The refactor is successful when:
1. The script becomes a thin trigger/orchestrator.
2. Common state behavior is centralized and reused.
3. Resume/retry from any stage is explicitly validated by precondition policy.
4. Existing flow behavior and outputs remain stable.
