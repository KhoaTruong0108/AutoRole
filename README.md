# AutoRole
Automate the job application flow

## Foundation Library
This project is built on top of SnapFlow as the core library foundation.

- SnapFlow repository: https://github.com/KhoaTruong0108/SnapFlow.git

## Real Data Run (No Mocks)
Use the real runner script to execute stages with live integrations and real websites:

- Script: `scripts/run_real_pipeline.py`
- Default mode is safe `observe` mode (it stops before submit)

### 1. Setup
```bash
python3 -m pip install -e ".[weasyprint]"
python3 -m pip install playwright
python3 -m playwright install chromium
```

Rendering engine default is `weasyprint`.

If you want to switch back to Pandoc + XeLaTeX later:
```bash
export AR_RENDERER__ENGINE="pandoc"
export AR_RENDERER__PANDOC_PATH="pandoc"
```
and ensure `xelatex` is installed and available on `PATH`.

Create your master resume file (default path):
```bash
mkdir -p ~/.autorole/resumes
test -f ~/.autorole/resumes/master.md || echo "# Master Resume" > ~/.autorole/resumes/master.md
```

Set your LLM key (choose one provider):
```bash
export OPENAI_API_KEY="..."
# or
export ANTHROPIC_API_KEY="..."
```

Use local Ollama provider (no cloud API key) if preferred:
```bash
export AR_LLM__PROVIDER="ollama"
export AR_LLM__OLLAMA_MODEL="llama3.1:8b"
export AR_LLM__OLLAMA_BASE_URL="http://127.0.0.1:11434"
```

Optional: profile data for form-intelligence stage:
```bash
mkdir -p ~/.autorole
cat > ~/.autorole/user_profile.json <<'JSON'
{
	"email": "me@example.com",
	"phone": "+1-555-0100",
	"country": "US"
}
JSON
```

### 2. Run In Observe Mode (Recommended First)
This runs: exploring -> scoring -> tailoring(+gate loop) -> packaging -> session -> form_intelligence

```bash
PYTHONPATH=src python3 scripts/run_real_pipeline.py \
	--mode observe \
	--platforms linkedin,indeed \
	--keywords "python,backend,engineer" \
	--location "United States" \
	--max-listings 1
```

Manual single-job mode (new):
```bash
PYTHONPATH=src python3 scripts/run_real_pipeline.py \
	--mode observe \
	--job-url "https://www.linkedin.com/jobs/view/1234567890/"
```

Optional platform hint in manual mode:
```bash
PYTHONPATH=src python3 scripts/run_real_pipeline.py \
	--mode observe \
	--job-url "https://www.indeed.com/viewjob?jk=abc123" \
	--job-platform indeed
```

### 3. Run In Apply Mode (Actual Submission Attempt)
This includes form_submission and concluding.

```bash
PYTHONPATH=src python3 scripts/run_real_pipeline.py \
	--mode apply \
	--platforms linkedin,indeed \
	--keywords "python,backend,engineer" \
	--location "United States" \
	--max-listings 1
```

### 3.1 Run In Apply-Dryrun Mode (Stop Right After Submit Click)
This mode executes up to `_submit_form()` in `FormSubmissionStage`, then stops before concluding persistence.
If the submit control is missing or blocked on the target page, the run still completes with
`submission_status=submitted_dryrun_submit_failed` to indicate submit was attempted in dryrun mode.

```bash
python3 scripts/run_real_pipeline.py \
	--mode apply-dryrun \
	--job-url "https://jobs.lever.co/aircall/43905627-fa43-44ee-8c23-65aa3e4b52ce/" \
	--job-platform lever
```

### 4. Verify Results
```bash
PYTHONPATH=src python3 -m autorole.cli.main status
PYTHONPATH=src python3 -m autorole.cli.main status <run_id>
PYTHONPATH=src python3 -m autorole.cli.main score <run_id>
PYTHONPATH=src python3 -m autorole.cli.main diff <run_id>
```

### 4.1 Resume From Previous Run Checkpoint
Resume from a previously failed or interrupted run without restarting from exploring/scoring.

```bash
PYTHONPATH=src python3 scripts/run_real_pipeline.py \
	--resume-run-id <run_id> \
	--mode apply-dryrun
```

Force restart from a specific stage:

```bash
PYTHONPATH=src python3 scripts/run_real_pipeline.py \
	--resume-run-id <run_id> \
	--from-stage form_intelligence \
	--mode apply-dryrun
```

Supported stages for `--from-stage`:
`exploring`, `scoring`, `tailoring`, `packaging`, `session`, `form_intelligence`, `form_submission`, `concluding`.

### 5. Trace Logs
Each real runner execution now creates a trace log file under `~/.autorole/logs` and prints its path
at start/end of execution.

Example:
```text
Trace log: /Users/<you>/.autorole/logs/real_pipeline_YYYYMMDD_HHMMSS.log
```

### 6. Stage Output Artifacts
For each `run_id`, stage outputs are persisted under:

```text
~/.autorole/logs/runs/<run_id>/
```

`stage_outputs.md` is created per run and indexes generated files such as:
- scoring: criteria/matched/mismatched summaries + job description HTML
- tailoring: diff summary and resume metadata
- form_intelligence: questionnaire/form JSON + answered markdown form

The main trace log includes `RUN_ARTIFACT_INDEX` and `STAGE_ARTIFACT` lines with exact file paths,
so artifacts are directly linkable from trace entries.
