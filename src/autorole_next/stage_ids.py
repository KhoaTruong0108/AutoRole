from __future__ import annotations

import warnings

SCORING = "scoring"
TAILORING = "tailoring"
PACKAGING = "packaging"
SESSION = "session"
FORM_SCRAPER = "formScraper"
FIELD_COMPLETER = "fieldCompleter"
FORM_SUBMISSION = "formSubmission"
LLM_APPLYING = "llm_applying"
CONCLUDING = "concluding"

STAGE_ALIASES = {
    "form_intelligence": FORM_SCRAPER,
    "llm_field_completer": FIELD_COMPLETER,
    "form_scraper": FORM_SCRAPER,
    "field_completer": FIELD_COMPLETER,
    "form_submission": FORM_SUBMISSION,
    "llm_applying": LLM_APPLYING,
}

# Backward compatibility for old stage ids. Prefer `llm_applying` everywhere.
DEPRECATED_STAGE_ALIASES = {
    "apply": LLM_APPLYING,
    "applying": LLM_APPLYING,
    "llm_apply": LLM_APPLYING,
    "llm_apply_executor": LLM_APPLYING,
}


def canonical_stage_id(stage_id: str) -> str:
    if stage_id in STAGE_ALIASES:
        return STAGE_ALIASES[stage_id]
    if stage_id in DEPRECATED_STAGE_ALIASES:
        warnings.warn(
            f"Stage id '{stage_id}' is deprecated; use '{LLM_APPLYING}' instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return DEPRECATED_STAGE_ALIASES[stage_id]
    return stage_id
