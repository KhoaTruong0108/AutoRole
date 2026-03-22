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
python3 -m pip install -e .
python3 -m pip install playwright
python3 -m playwright install chromium
```

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

### 4. Verify Results
```bash
PYTHONPATH=src python3 -m autorole.cli.main status
PYTHONPATH=src python3 -m autorole.cli.main status <run_id>
PYTHONPATH=src python3 -m autorole.cli.main score <run_id>
PYTHONPATH=src python3 -m autorole.cli.main diff <run_id>
```
