# ScoringStage Design Notes

## Five Criteria and Default Weights

Scoring uses five criteria, each in the range [0, 1]:

- technical_skills: 0.30
- experience_depth: 0.25
- seniority_alignment: 0.20
- domain_relevance: 0.15
- culture_fit: 0.10

Overall score uses a weighted sum with normalized weights:

overall_score = sum(criteria_scores[k] * normalized_weights[k])

## Prompt Structure

Scoring uses two structured LLM calls.

1. JD parse call
- System prompt: parse JD text into a strict JSON schema:
  - qualifications
  - responsibilities
  - required_skills
  - preferred_skills
  - culture_signals
- User prompt: plain extracted text from JD HTML.

2. Criteria scoring call
- System prompt: score all five criteria and return strict JSON:
  - scores: criterion -> float
  - details: criterion -> {score, matched, gaps}
- User prompt includes:
  - the parsed JD breakdown JSON
  - full resume markdown

## jd_html Caching Across Loop Iterations

ScoringStage fetches JD HTML only on first pass.

- First pass: if ctx.score is None or ctx.score.jd_html is empty, fetch via Playwright page.goto() then page.content().
- Loop re-entry: if ctx.score.jd_html exists, skip navigation and reuse cached HTML.

This avoids repeated page fetches during Tailoring <-> Scoring loops.

## CriterionScores Worked Example

Example structured response:

```json
{
  "scores": {
    "technical_skills": 0.85,
    "experience_depth": 0.72,
    "seniority_alignment": 0.90,
    "domain_relevance": 0.60,
    "culture_fit": 0.78
  },
  "details": {
    "technical_skills": {
      "score": 0.85,
      "matched": ["Python", "Kubernetes"],
      "gaps": ["Rust"]
    },
    "experience_depth": {
      "score": 0.72,
      "matched": ["Led backend systems"],
      "gaps": ["ML infra at scale"]
    },
    "seniority_alignment": {
      "score": 0.90,
      "matched": ["Staff-level ownership"],
      "gaps": []
    },
    "domain_relevance": {
      "score": 0.60,
      "matched": ["B2B SaaS"],
      "gaps": ["Regulated fintech"]
    },
    "culture_fit": {
      "score": 0.78,
      "matched": ["Cross-functional collaboration"],
      "gaps": ["Open-source leadership"]
    }
  }
}
```

Matched/mismatched lists in ScoreReport are derived from criterion detail scores:

- matched: score >= 0.7
- mismatched: score < 0.7
