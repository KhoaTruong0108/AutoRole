# ExploringStage Design Notes

## Scope

`ExploringStage` is the fan-out entry stage. It reads a search config, queries one scraper per platform, and emits one `JobApplicationContext` per discovered listing.

## LinkedIn Card Parsing

The scraper navigates to LinkedIn search results and waits for `.jobs-search-results__list`.

Primary selectors used:
- Card: `.job-card-container`
- Title: `.job-card-list__title`
- Company: `.job-card-container__company-name`
- Link: first `a` in the card

Extracted fields:
- `job_title`: title node text
- `company_name`: company node text
- `job_url`: href from the anchor
- `job_id`: digit sequence extracted from URL path fallback
- `platform`: `linkedin`
- `crawled_at`: current UTC timestamp

## Jitter / Rate Limiting

A per-card jitter is applied between parsed cards:
- Default range: `800ms` to `2000ms`
- Implementation: `await asyncio.sleep(random.uniform(*jitter_ms) / 1000)`

This reduces bursty scraping behavior and aligns with conservative crawling.

## Deduplication Strategy

`ExploringStage` computes:
- `run_id = {company_name_lower_snake}_{job_id}`

Examples:
- `Acme Corp` + `456` -> `acme_corp_456`

At persistence time, SnapFlow store semantics (`ON CONFLICT DO NOTHING` / idempotent save) prevent duplicate run records for repeated discoveries of the same listing key.

## Failure Isolation

A single failing platform scraper must not fail the entire stage.

Behavior:
- Scraper exception is logged as warning.
- Stage continues processing remaining platforms.
- Stage fails with `NoListingsFound` only if aggregate results are empty.
