from __future__ import annotations

from autorole_next.stage_ids import (
	DEPRECATED_STAGE_ALIASES,
	FIELD_COMPLETER,
	FORM_SCRAPER,
	LLM_APPLYING,
	STAGE_ALIASES,
)
from autorole_next.tui.view_models import resolve_stage_label


def test_stage_aliases_resolve_to_new_names() -> None:
	assert STAGE_ALIASES["form_intelligence"] == FORM_SCRAPER
	assert STAGE_ALIASES["llm_field_completer"] == FIELD_COMPLETER
	assert STAGE_ALIASES["llm_applying"] == LLM_APPLYING
	assert DEPRECATED_STAGE_ALIASES["llm_apply"] == LLM_APPLYING
	assert resolve_stage_label("form_intelligence") == "Form Scraper"
	assert resolve_stage_label("llm_field_completer") == "Field Completer"
	assert resolve_stage_label("llm_apply") == "LLM Applying"