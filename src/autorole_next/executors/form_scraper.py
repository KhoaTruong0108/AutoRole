from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .._snapflow import Executor, StageResult, StateContext


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FormScraperExecutor(Executor[dict[str, Any]]):
    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        listing = payload.get("listing") if isinstance(payload.get("listing"), dict) else {}

        apply_url = str(listing.get("apply_url") or listing.get("job_url") or "")
        platform = str(listing.get("platform", "unknown"))

        fields = [
            {"name": "first_name", "required": True, "type": "text"},
            {"name": "last_name", "required": True, "type": "text"},
            {"name": "email", "required": True, "type": "email"},
            {"name": "resume", "required": True, "type": "file"},
        ]

        form_payload = {
            "apply_url": apply_url,
            "platform": platform,
            "page_index": 0,
            "page_label": "Application Form",
            "extracted_fields": fields,
            "generated_at": _utcnow_iso(),
        }

        # Keep both keys during migration to preserve compatibility with legacy naming.
        payload["formScraper"] = form_payload
        payload["form_intelligence"] = form_payload

        return StageResult.ok(payload)
