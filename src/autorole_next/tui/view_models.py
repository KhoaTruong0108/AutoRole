from __future__ import annotations

from ..stage_ids import (
    CONCLUDING,
    FIELD_COMPLETER,
    FORM_SCRAPER,
    FORM_SUBMISSION,
    LLM_APPLYING,
    PACKAGING,
    SCORING,
    SESSION,
    TAILORING,
    canonical_stage_id,
)


STAGE_LABELS = {
    SCORING: "Scoring",
    TAILORING: "Tailoring",
    PACKAGING: "Packaging",
    SESSION: "Session",
    FORM_SCRAPER: "Form Scraper",
    FIELD_COMPLETER: "Field Completer",
    FORM_SUBMISSION: "Form Submission",
    LLM_APPLYING: "LLM Applying",
    CONCLUDING: "Concluding",
}


def resolve_stage_label(stage_id: str) -> str:
    normalized_stage_id = canonical_stage_id(stage_id)
    return STAGE_LABELS.get(normalized_stage_id, normalized_stage_id.replace("_", " ").title())
