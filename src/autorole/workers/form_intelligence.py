from __future__ import annotations

import json

from autorole.context import JobApplicationContext
from autorole.workers.base import StageWorker


class FormIntelligenceWorker(StageWorker):
    name = "form_intelligence"

    async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = attempt
        fi = ctx.form_intelligence
        if fi is None:
            return
        page_label = fi.page_label.replace(" ", "_")[:30] if fi.page_label else "page"
        self._write_artifact(
            f"page_{fi.page_index}_{page_label}_fields.json",
            json.dumps([item.model_dump(mode="json") for item in fi.extracted_fields], indent=2) + "\n",
            ctx.run_id,
        )
        self._write_artifact(
            f"page_{fi.page_index}_{page_label}_instructions.json",
            json.dumps([item.model_dump(mode="json") for item in fi.fill_instructions], indent=2) + "\n",
            ctx.run_id,
        )

    def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = attempt
        fi = ctx.form_intelligence
        if fi is None:
            return
        print(
            f"[ok] form_intelligence -> page={fi.page_index} "
            f"fields={len(fi.extracted_fields)} label={fi.page_label!r}"
        )
