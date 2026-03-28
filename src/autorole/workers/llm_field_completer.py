from __future__ import annotations

import json

from autorole.context import JobApplicationContext
from autorole.workers.base import StageWorker


class LLMFieldCompleterWorker(StageWorker):
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
