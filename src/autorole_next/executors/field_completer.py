from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autorole.config import AppConfig
from autorole.integrations.llm import AnthropicLLMClient, OllamaLLMClient, OpenAILLMClient

from .._snapflow import Executor, StageResult, StateContext
from ..form_controls.exceptions import MappingError
from ..form_controls.mapper import AIFieldMapper
from ..form_controls.models import ExtractedField, FillInstruction
from ..form_controls.profile import load_profile


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FieldCompleterExecutor(Executor[dict[str, Any]]):
    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        metadata = dict(ctx.metadata)
        listing = payload.get("listing") if isinstance(payload.get("listing"), dict) else None
        form_payload = payload.get("formScraper") if isinstance(payload.get("formScraper"), dict) else {}
        extracted_fields = form_payload.get("extracted_fields") if isinstance(form_payload.get("extracted_fields"), list) else None
        if listing is None or extracted_fields is None:
            return StageResult.fail(
                "FieldCompleterExecutor: listing and formScraper.extracted_fields must be set",
                "PreconditionError",
            )

        page_index = int(form_payload.get("page_index", 0))

        try:
            fields = [
                ExtractedField.model_validate(
                    _coerce_extracted_field_payload(
                        field,
                        run_id=ctx.correlation_id,
                        page_index=page_index,
                        page_label=str(form_payload.get("page_label", "Application Form")),
                    )
                )
                for field in extracted_fields
            ]
        except Exception as exc:
            return StageResult.fail(f"Invalid extracted fields payload: {exc}", "PreconditionError")

        use_random_answers = bool(metadata.get("use_random_questionnaire_answers", False))
        if use_random_answers:
            instructions_obj = _build_random_instructions(fields, ctx.correlation_id, page_index)
        else:
            config = AppConfig()
            profile_path = Path(
                str(metadata.get("profile_path") or (Path(config.base_dir).expanduser() / "user_profile.json"))
            )
            if not profile_path.exists():
                return StageResult.fail(f"user profile not found: {profile_path}", "ConfigError")
            try:
                profile = load_profile(profile_path)
            except Exception as exc:
                return StageResult.fail(f"Failed to load user profile: {exc}", "ConfigError")

            try:
                mapper = AIFieldMapper(_build_llm_client(config))
                instructions_obj = await mapper.map(fields, profile, ctx.correlation_id, page_index)
            except MappingError as exc:
                return StageResult.fail(str(exc), "MappingError")
            except Exception as exc:
                return StageResult.fail(f"Mapping failed: {exc}", "MappingError")

        instructions = [inst.model_dump(mode="json") if hasattr(inst, "model_dump") else dict(inst) for inst in instructions_obj]

        # Keep migration compatibility by updating instructions in both naming schemes.
        form_payload_with_instructions = dict(form_payload)
        form_payload_with_instructions["fill_instructions"] = instructions
        payload["formScraper"] = form_payload_with_instructions
        payload["form_intelligence"] = form_payload_with_instructions

        form_session = payload.get("form_session") if isinstance(payload.get("form_session"), dict) else None
        if isinstance(form_session, dict):
            all_instructions = form_session.get("all_instructions") if isinstance(form_session.get("all_instructions"), list) else []
            all_instructions.extend(instructions)
            form_session["all_instructions"] = all_instructions
            payload["form_session"] = form_session

        result_payload = {
            "page_index": page_index,
            "page_label": str(form_payload.get("page_label", "Application Form")),
            "fill_instructions": instructions,
            "generated_at": _utcnow_iso(),
            "questionnaire": list(form_payload.get("questionnaire", [])) if isinstance(form_payload.get("questionnaire"), list) else [],
            "form_json_filled": dict(form_payload.get("form_json_filled", {})) if isinstance(form_payload.get("form_json_filled"), dict) else {},
        }

        # Keep both keys during migration to preserve compatibility with legacy naming.
        payload["fieldCompleter"] = result_payload
        payload["llm_field_completer"] = result_payload

        return StageResult.ok(payload)



def _build_llm_client(config: AppConfig) -> Any:
    provider = str(config.llm.provider).lower()
    if provider == "openai":
        return OpenAILLMClient(config.llm)
    if provider == "anthropic":
        return AnthropicLLMClient(config.llm)
    return OllamaLLMClient(config.llm)


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
        if field.field_type in {"select", "radio", "combobox_lazy", "combobox_search"}:
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


def _coerce_extracted_field_payload(
    field: Any,
    *,
    run_id: str,
    page_index: int,
    page_label: str,
) -> dict[str, Any]:
    raw = dict(field) if isinstance(field, dict) else {}
    field_id = str(raw.get("id") or raw.get("name") or "unknown")
    raw_field_type = str(raw.get("field_type") or raw.get("type") or "unknown").lower()
    field_type = raw_field_type
    if field_type not in {
        "text",
        "textarea",
        "select",
        "radio",
        "checkbox",
        "combobox_search",
        "combobox_lazy",
        "date",
        "file",
        "hidden",
        "unknown",
    }:
        field_type = "text"
    label = str(raw.get("label") or field_id)
    selector = str(raw.get("selector") or f"[id='{field_id}']")

    return {
        "id": field_id,
        "run_id": str(raw.get("run_id") or run_id),
        "page_index": int(raw.get("page_index", page_index)),
        "page_label": str(raw.get("page_label") or page_label),
        "field_type": field_type,
        "selector": selector,
        "label": label,
        "required": bool(raw.get("required", False)),
        "options": raw.get("options") if isinstance(raw.get("options"), list) else [],
        "prefilled_value": str(raw.get("prefilled_value") or ""),
        "aria_role": str(raw.get("aria_role") or ""),
        "extraction_source": str(raw.get("extraction_source") or "dom"),
    }
