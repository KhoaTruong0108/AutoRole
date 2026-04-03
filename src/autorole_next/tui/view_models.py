from __future__ import annotations

from ..stage_ids import FIELD_COMPLETER, FORM_SCRAPER, PACKAGING, SCORING, STAGE_ALIASES, TAILORING


STAGE_LABELS = {
    SCORING: "Scoring",
    TAILORING: "Tailoring",
    PACKAGING: "Packaging",
    FORM_SCRAPER: "Form Scraper",
    FIELD_COMPLETER: "Field Completer",
}


def resolve_stage_label(stage_id: str) -> str:
    canonical_stage_id = STAGE_ALIASES.get(stage_id, stage_id)
    return STAGE_LABELS.get(canonical_stage_id, canonical_stage_id.replace("_", " ").title())
