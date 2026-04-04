from __future__ import annotations

import asyncio

from autorole_next._snapflow import StateContext
from autorole_next.executors.field_completer import FieldCompleterExecutor


def _ctx(data: dict[str, object], metadata: dict[str, object] | None = None) -> StateContext[dict[str, object]]:
    return StateContext[dict[str, object]](
        correlation_id="corr-1",
        current_stage="fieldCompleter",
        data=data,
        metadata={} if metadata is None else metadata,
    )


def test_field_completer_executor_fails_without_required_inputs() -> None:
    executor = FieldCompleterExecutor()

    result = asyncio.run(executor.execute(_ctx({"formScraper": {"extracted_fields": []}})))

    assert result.success is False
    assert result.error_type == "PreconditionError"


def test_field_completer_executor_maps_fields_and_updates_compatibility_payloads() -> None:
    executor = FieldCompleterExecutor()
    form_payload = {
        "page_index": 2,
        "page_label": "Equal Opportunity",
        "extracted_fields": [
            {"id": "country", "field_type": "select", "required": True, "options": ["US", "CA"]},
            {"id": "portfolio", "field_type": "file", "required": False},
            {"id": "consent", "field_type": "checkbox", "required": False, "options": ["yes"]},
            {"id": "email", "field_type": "email", "required": True},
            {"id": "phone", "field_type": "phone", "required": True},
            {"id": "bio", "field_type": "textarea", "required": False},
        ],
        "questionnaire": [{"q": "veteran"}],
        "form_json_filled": {"legacy": True},
    }

    result = asyncio.run(
        executor.execute(
            _ctx(
                {
                    "listing": {"platform": "workday"},
                    "formScraper": form_payload,
                    "form_session": {"all_instructions": [{"field_id": "existing"}]},
                },
                metadata={"use_random_questionnaire_answers": True},
            )
        )
    )

    assert result.success is True
    output = dict(result.data)

    completion = output.get("fieldCompleter")
    assert isinstance(completion, dict)
    instructions = completion.get("fill_instructions")
    assert isinstance(instructions, list)
    assert len(instructions) == 6

    by_field = {str(item["field_id"]): item for item in instructions}
    assert by_field["country"]["action"] == "fill"
    assert by_field["country"]["value"] == "US"
    assert by_field["country"]["source"] == "profile_inferred"

    assert by_field["portfolio"]["action"] == "skip"
    assert by_field["portfolio"]["source"] == "no_match"

    assert by_field["consent"]["action"] == "fill"
    assert by_field["consent"]["value"] == "yes"

    assert by_field["email"]["value"] == "Test Value"
    assert by_field["phone"]["value"] == "Test Value"
    assert by_field["bio"]["value"] == "Test Value"

    assert output.get("llm_field_completer") == completion

    form_scraper = output.get("formScraper")
    assert isinstance(form_scraper, dict)
    assert form_scraper.get("fill_instructions") == instructions
    assert output.get("form_intelligence") == form_scraper

    form_session = output.get("form_session")
    assert isinstance(form_session, dict)
    all_instructions = form_session.get("all_instructions")
    assert isinstance(all_instructions, list)
    assert len(all_instructions) == 7

    assert completion.get("questionnaire") == [{"q": "veteran"}]
    assert completion.get("form_json_filled") == {"legacy": True}
