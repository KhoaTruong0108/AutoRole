# AutoRole CLI

This folder contains the Typer-based command line interface for AutoRole.

## Entry Points

You can invoke the CLI through any of these entry points:

```bash
PYTHONPATH=src python -m autorole
PYTHONPATH=src python -m autorole.cli.main
ar
```

`python -m autorole` works because the module entry point is defined in [__main__.py](../__main__.py).

## Top-Level Commands

### run

Runs the daily pipeline entrypoint.

```bash
PYTHONPATH=src python -m autorole run
```

### status

Shows recent runs, or detailed status for one run when a `run_id` is provided.

```bash
PYTHONPATH=src python -m autorole status
PYTHONPATH=src python -m autorole status <run_id>
```

### blocked

Lists blocked or errored runs from the pipeline state tables.

```bash
PYTHONPATH=src python -m autorole blocked
```

### resume

Requests pipeline resume for a run, optionally from a specific stage.

```bash
PYTHONPATH=src python -m autorole resume <run_id>
PYTHONPATH=src python -m autorole resume <run_id> --from-stage scoring
```

### diff

Shows the latest tailoring diff summary for a run.

```bash
PYTHONPATH=src python -m autorole diff <run_id>
PYTHONPATH=src python -m autorole diff <run_id> --full
```

### score

Shows the latest score report summary for a run.

```bash
PYTHONPATH=src python -m autorole score <run_id>
```

### prune

Deletes old generated files based on the retention policy.

```bash
PYTHONPATH=src python -m autorole prune
```

### tui

Launches the Textual terminal UI.

```bash
PYTHONPATH=src python -m autorole tui
```

## Credentials Commands

### credentials set

Stores a credential value using the configured credential store.

```bash
PYTHONPATH=src python -m autorole credentials set <key>
```

### credentials delete

Deletes a stored credential.

```bash
PYTHONPATH=src python -m autorole credentials delete <key>
```

## Queue Commands

### queue sql

Prints a SQLite query you can use to inspect persisted queue rows.

Examples:

```bash
PYTHONPATH=src python -m autorole queue sql scoring_q
PYTHONPATH=src python -m autorole queue sql dead_letter_q --all --payload --limit 50
PYTHONPATH=src python -m autorole queue sql scoring_q --run-id <run_id>
PYTHONPATH=src python -m autorole queue sql scoring_q --message-id <message_id>
```

Notes:

- `--visible-only` is the default.
- Use `--all` to include non-visible queued rows or processing rows.
- `--payload` adds the payload and metadata columns to the generated SQL.

### queue redrive

Moves dead-letter messages back to their canonical input queue.

Supported modes:

- Redrive one message by `message_id`
- Redrive all dead-letter messages that belong to a target input queue

Examples:

```bash
PYTHONPATH=src python -m autorole queue redrive --message-id <message_id>
PYTHONPATH=src python -m autorole queue redrive --queue-name scoring_q
```

Supported queue names for `--queue-name`:

- `exploring_q`
- `scoring_q`
- `tailoring_q`
- `packaging_q`
- `session_q`
- `form_intel_q`
- `llm_field_completer_q`
- `form_sub_q`
- `concluding_q`

## Source Files

- [main.py](main.py): Typer command registration and command implementations
- [tui.py](tui.py): Textual terminal UI
- [__init__.py](__init__.py): package marker
- [../__main__.py](../__main__.py): `python -m autorole` entry point