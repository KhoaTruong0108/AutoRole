# TailoringStage Design Notes

## Tailoring Degrees and Guardrails

TailoringStage selects one of five degrees based on score thresholds.

- Degree 0 (pass-through): no modifications.
- Degree 1 (emphasis): reorder/reword existing evidence only.
- Degree 2 (inflation): conservative metric strengthening; do not alter company or timeline facts.
- Degree 3 (projection): plausible project framing from real experience; company and timeline remain intact.
- Degree 4 (reinvention): last resort only and requires explicit user acknowledgement.

## Degree Selection Thresholds

Default table:

- score >= 0.85 -> degree 0
- 0.70 <= score < 0.85 -> degree 1
- 0.55 <= score < 0.70 -> degree 2
- 0.40 <= score < 0.55 -> degree 3
- score < 0.40 and degree_4_enabled=True -> degree 4
- score < 0.40 and degree_4_enabled=False -> block

## Diff Mapping Strategy

_compute_diff compares source and tailored markdown using line-level diffing.

- Removed lines become DiffChange with change_type=removed.
- Added lines become DiffChange with change_type=added.
- Criterion mapping is inferred from keyword heuristics and JD context:
  - technical_skills: technology keywords
  - seniority_alignment: lead/senior/staff keywords
  - experience_depth: scale/years/production keywords
  - domain_relevance: industry/domain keywords
  - culture_fit: fallback from JD culture signals

The resulting DiffReport is serialized into TailoredResume.diff_summary JSON.

## BestFitGate Case Table

BestFitGate uses five deterministic decisions:

1. tailoring_degree == 0
- Decision: PASS
- Meaning: score already strong enough; continue to Packaging.

2. previous_score missing (first tailoring)
- Decision: LOOP to scoring
- Reason format: first_tailoring|baseline=X.XXXX
- Meaning: re-score tailored resume against cached JD baseline.

3. attempt >= max_attempts
- Decision: BLOCK
- Meaning: stop loop to avoid endless retries.

4. current_score > previous_score
- Decision: LOOP to scoring
- Meaning: keep improving while attempts remain.

5. current_score <= previous_score
- Decision: BLOCK
- Reason includes stagnated or regressed.

## Worked Decision Examples

- Example A: degree 0 after score 0.88 -> PASS.
- Example B: first tailored score 0.72 with no baseline -> LOOP and reason includes baseline=0.7200.
- Example C: baseline 0.72, current 0.80, attempt 2 of 3 -> LOOP (improved).
- Example D: baseline 0.72, current 0.72 -> BLOCK (stagnated).
- Example E: baseline 0.72, current 0.69 -> BLOCK (regressed).
