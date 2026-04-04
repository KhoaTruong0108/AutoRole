from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .._snapflow import Executor, StageResult, StateContext


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FieldCompleterExecutor(Executor[dict[str, Any]]):
    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        form_payload = payload.get("formScraper") if isinstance(payload.get("formScraper"), dict) else {}
        extracted_fields = form_payload.get("extracted_fields") if isinstance(form_payload.get("extracted_fields"), list) else []

        instructions = [
            {
                "field": str(field.get("name", "unknown")),
                "value": f"auto-filled-{idx + 1}",
                "confidence": 0.9,
            }
            for idx, field in enumerate(extracted_fields)
        ]

        result_payload = {
            "page_index": int(form_payload.get("page_index", 0)),
            "page_label": str(form_payload.get("page_label", "Application Form")),
            "fill_instructions": instructions,
            "generated_at": _utcnow_iso(),
        }

        # Keep both keys during migration to preserve compatibility with legacy naming.
        payload["fieldCompleter"] = result_payload
        payload["llm_field_completer"] = result_payload

        return StageResult.ok(payload)
