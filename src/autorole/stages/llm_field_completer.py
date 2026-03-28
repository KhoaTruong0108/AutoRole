from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autorole.config import AppConfig
from autorole.context import JobApplicationContext, LLMFieldCompletionResult
from autorole.integrations.form_controls.exceptions import MappingError
from autorole.integrations.form_controls.mapper import AIFieldMapper
from autorole.integrations.form_controls.models import ExtractedField, FillInstruction
from autorole.integrations.form_controls.profile import load_profile
from autorole.integrations.llm import LLMClient
from autorole.stage_base import AutoRoleStage

try:
    from pipeline.interfaces import Stage
    from pipeline.types import Message, StageResult
except Exception:
    class Stage:
        async def execute(self, message: "Message") -> "StageResult":
            raise NotImplementedError

    class Message:
        def __init__(self, run_id: str, payload: Any, metadata: dict[str, Any] | None = None) -> None:
            self.run_id = run_id
            self.payload = payload
            self.metadata = metadata or {}

    class StageResult:
        def __init__(
            self,
            success: bool,
            output: Any = None,
            error: str | None = None,
            error_type: str | None = None,
        ) -> None:
            self.success = success
            self.output = output
            self.error = error
            self.error_type = error_type

        @classmethod
        def ok(cls, output: Any) -> "StageResult":
            return cls(success=True, output=output)

        @classmethod
        def fail(cls, error: str, error_type: str = "") -> "StageResult":
            return cls(success=False, error=error, error_type=error_type)


class LLMFieldCompleterStage(Stage):
    name = "llm_field_completer"
    concurrency = 1

    def __init__(
        self,
        config: AppConfig,
        llm_client: LLMClient,
        field_mapper: Any | None = None,
        use_random_questionnaire_answers: bool = False,
    ) -> None:
        self._config = config
        self._mapper = field_mapper or AIFieldMapper(llm_client)
        self._use_random_questionnaire_answers = use_random_questionnaire_answers

    async def execute(self, message: Message) -> StageResult:
        ctx = JobApplicationContext.model_validate(message.payload)
        if (
            ctx.listing is None
            or ctx.packaged is None
            or ctx.form_session is None
            or ctx.form_intelligence is None
        ):
            return StageResult.fail(
                "LLMFieldCompleterStage: listing, packaged, form_session and form_intelligence must be set",
                "PreconditionError",
            )

        profile_path = Path(self._config.base_dir).expanduser() / "user_profile.json"
        if not profile_path.exists():
            return StageResult.fail("user_profile.json not found", "ConfigError")

        try:
            profile = load_profile(profile_path)
        except Exception as exc:
            return StageResult.fail(f"Failed to load user profile: {exc}", "ConfigError")

        fi = ctx.form_intelligence
        fields = fi.extracted_fields

        try:
            instructions = await self._map_fields(fields, profile, message.run_id, fi.page_index)
        except MappingError as exc:
            return StageResult.fail(str(exc), "MappingError")
        except Exception as exc:
            return StageResult.fail(f"Mapping failed: {exc}", "MappingError")

        completion = LLMFieldCompletionResult(
            page_index=fi.page_index,
            page_label=fi.page_label,
            fill_instructions=instructions,
            generated_at=datetime.now(timezone.utc),
            questionnaire=fi.questionnaire,
            form_json_filled=fi.form_json_filled,
        )

        session = ctx.form_session
        session.all_instructions.extend(instructions)

        # Backward compatibility for callers still reading instructions from form_intelligence.
        fi_with_instructions = fi.model_copy(update={"fill_instructions": instructions})

        return StageResult.ok(
            ctx.model_copy(
                update={
                    "form_session": session,
                    "form_intelligence": fi_with_instructions,
                    "llm_field_completion": completion,
                }
            )
        )

    async def _map_fields(
        self,
        fields: list[ExtractedField],
        profile: Any,
        run_id: str,
        page_index: int,
    ) -> list[FillInstruction]:
        if self._use_random_questionnaire_answers:
            return _build_random_instructions(fields, run_id, page_index)
        return await self._mapper.map(fields, profile, run_id, page_index)


class LLMFieldCompleterExecutor(AutoRoleStage):
    name = "llm_field_completer"

    async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = attempt
        completion = ctx.llm_field_completion
        if completion is None:
            return
        page_label = completion.page_label.replace(" ", "_")[:30] if completion.page_label else "page"
        self._write_artifact(
            f"page_{completion.page_index}_{page_label}_instructions.json",
            json.dumps([item.model_dump(mode="json") for item in completion.fill_instructions], indent=2)
            + "\n",
            ctx.run_id,
        )

    def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = attempt
        completion = ctx.llm_field_completion
        if completion is None:
            return
        print(
            f"[ok] llm_field_completer -> page={completion.page_index} "
            f"instructions={len(completion.fill_instructions)} label={completion.page_label!r}"
        )


def _build_random_instructions(
    fields: list[ExtractedField],
    run_id: str,
    page_index: int,
) -> list[FillInstruction]:
    instructions: list[FillInstruction] = []
    for field in fields:
        value: str | None
        action = "fill"
        source = "generated"
        if field.field_type in {"select", "radio", "combobox_lazy"}:
            if field.options:
                value = field.options[0]
                source = "profile_inferred"
            elif field.required:
                value = "N/A"
            else:
                action = "skip"
                value = None
                source = "no_match"
        elif field.field_type == "checkbox":
            value = ",".join(field.options[:1]) if field.options else ""
        elif field.field_type in {"hidden", "file"}:
            action = "skip"
            value = None
            source = "no_match"
        else:
            value = "Test Value"
        instructions.append(
            FillInstruction(
                field_id=field.id,
                run_id=run_id,
                action=action,
                value=value,
                source=source,
                page_index=page_index,
            )
        )
    return instructions
