from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autorole.context import FormIntelligenceResult, FormSession, JobApplicationContext, PackagedResume
from autorole.integrations.form_controls.models import DetectionResult, ExtractedField, FillInstruction
from autorole.stages.llm_field_completer import LLMFieldCompleterStage
from tests.conftest import SAMPLE_LISTING

try:
    from pipeline.types import Message
except Exception:
    class Message:  # pragma: no cover
        def __init__(self, run_id: str, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
            self.run_id = run_id
            self.payload = payload
            self.metadata = metadata or {}


class StubMapper:
    def __init__(self, instructions: list[FillInstruction]) -> None:
        self.instructions = instructions
        self.calls = 0

    async def map(self, *args: Any, **kwargs: Any) -> list[FillInstruction]:
        _ = (args, kwargs)
        self.calls += 1
        return self.instructions


def _field() -> ExtractedField:
    return ExtractedField(
        id="field-1",
        run_id="acme_123",
        page_index=0,
        page_label="Application form",
        field_type="text",
        selector="[name='email']",
        label="Email",
        required=True,
        options=[],
        prefilled_value="",
    )


def _instruction() -> FillInstruction:
    return FillInstruction(
        field_id="field-1",
        run_id="acme_123",
        action="fill",
        value="me@example.com",
        source="generated",
        page_index=0,
    )


def _ctx() -> JobApplicationContext:
    return JobApplicationContext(
        run_id="acme_123",
        listing=SAMPLE_LISTING,
        packaged=PackagedResume(
            resume_id="res-1",
            pdf_path="/tmp/resume.pdf",
            packaged_at=datetime.now(timezone.utc),
        ),
        form_session=FormSession(
            detection=DetectionResult(
                run_id="acme_123",
                platform_id="generic",
                apply_url="https://example.com/apply",
                used_iframe=False,
                detection_method="fallback",
            ),
            page_index=0,
        ),
        form_intelligence=FormIntelligenceResult(
            page_index=0,
            page_label="Application form",
            extracted_fields=[_field()],
            fill_instructions=[],
            generated_at=datetime.now(timezone.utc),
        ),
    )


async def test_llm_field_completer_maps_fields_and_updates_context(test_config: Any) -> None:
    base_dir = Path(test_config.base_dir)
    (base_dir / "user_profile.json").write_text("{}", encoding="utf-8")

    mapper = StubMapper([_instruction()])
    stage = LLMFieldCompleterStage(test_config, llm_client=object(), field_mapper=mapper)

    result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

    assert result.success
    out_ctx = JobApplicationContext.model_validate(result.output)
    assert out_ctx.llm_field_completion is not None
    assert len(out_ctx.llm_field_completion.fill_instructions) == 1
    assert out_ctx.form_intelligence is not None
    assert len(out_ctx.form_intelligence.fill_instructions) == 1
    assert out_ctx.form_session is not None
    assert len(out_ctx.form_session.all_instructions) == 1
    assert mapper.calls == 1


async def test_llm_field_completer_fails_when_profile_missing(test_config: Any) -> None:
    mapper = StubMapper([_instruction()])
    stage = LLMFieldCompleterStage(test_config, llm_client=object(), field_mapper=mapper)

    result = await stage.execute(Message(run_id="acme_123", payload=_ctx().model_dump()))

    assert not result.success
    assert result.error_type == "ConfigError"


async def test_llm_field_completer_fails_without_preconditions(test_config: Any) -> None:
    stage = LLMFieldCompleterStage(test_config, llm_client=object(), field_mapper=StubMapper([]))

    result = await stage.execute(Message(run_id="acme_123", payload=JobApplicationContext(run_id="acme_123").model_dump()))

    assert not result.success
    assert result.error_type == "PreconditionError"
