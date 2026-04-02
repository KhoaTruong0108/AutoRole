# Observation and Debug Guide

This guide helps you manually verify artifacts after running:

```bash
python3 ./scripts/run_real_pipeline.py --job-url <JOB_URL> --job-platform <PLATFORM>
```

## 1. Check recent runs from CLI

```bash
PYTHONPATH=src python -m autorole.cli.main status
```

Look for the latest `run_id` in the output. Example from a recent run:

- `aircall_439056274344823653452`

## 2. Verify database records (SQLite)

Open the database:

```bash
sqlite3 ~/.autorole/pipeline.db
```

Check latest listing records:

```sql
SELECT run_id, company_name, job_title, platform, crawled_at
FROM job_listings
ORDER BY crawled_at DESC
LIMIT 5;
```

Check latest score records:

```sql
SELECT run_id, resume_id, attempt, overall_score, scored_at
FROM score_reports
ORDER BY scored_at DESC
LIMIT 5;
```

Check latest tailored resume records:

```sql
SELECT run_id, resume_id, tailoring_degree, file_path, tailored_at
FROM tailored_resumes
ORDER BY tailored_at DESC
LIMIT 5;
```

Check one specific run:

```sql
SELECT run_id, company_name, job_title, platform
FROM job_listings
WHERE run_id = 'aircall_439056274344823653452';

SELECT run_id, overall_score, attempt
FROM score_reports
WHERE run_id = 'aircall_439056274344823653452'
ORDER BY attempt DESC;

SELECT run_id, file_path, tailoring_degree
FROM tailored_resumes
WHERE run_id = 'aircall_439056274344823653452'
ORDER BY tailored_at DESC
LIMIT 1;
```

Exit SQLite:

```sql
.quit
```

## 3. Verify generated markdown resume artifact

List latest resumes:

```bash
ls -lt ~/.autorole/resumes | head
```

Open the generated markdown file:

```bash
cat ~/.autorole/resumes/<generated_resume_file>.md
```

Or open in VS Code:

```bash
code ~/.autorole/resumes/<generated_resume_file>.md
```

## 4. Optional score/diff verification

```bash
PYTHONPATH=src python -m autorole.cli.main score <run_id>
PYTHONPATH=src python -m autorole.cli.main diff <run_id>
```

## 5. Expected records by run mode

- Observe mode:
  - Expected: `job_listings`, `score_reports`, `tailored_resumes`
  - Not guaranteed: final `job_applications` submission row
- Apply mode:
  - Expected: records above plus submission-related data when downstream stages succeed
