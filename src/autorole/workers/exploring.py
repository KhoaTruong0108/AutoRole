from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from autorole.context import JobApplicationContext
from autorole.queue import PACKAGING_Q, Message, QueueBackend
from autorole.workers.base import StageWorker


class ExploringWorker(StageWorker):
    name = "exploring"

    def __init__(self, *args: object, on_fanout: Callable[[int], None] | None = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._on_fanout = on_fanout

    async def process(self, queue: QueueBackend, msg: Message) -> None:
        result = await self._execute_inner(msg)
        if result is None:
            delay = self._backoff(msg.attempt)
            await queue.nack(self._config.input_queue, msg.message_id, delay)
            self._logger.exception("unhandled exception stage=%s run_id=%s", self.name, msg.run_id)
            return

        if not getattr(result, "success", False):
            await queue.enqueue(msg.dead_letter_queue, msg)
            await queue.ack(self._config.input_queue, msg.message_id)
            self._logger.warning(
                "blocked stage=%s run_id=%s reason=%s",
                self.name,
                msg.run_id,
                getattr(result, "error", "exploring_failed"),
            )
            if self._on_block is not None:
                self._on_block(msg.run_id, str(getattr(result, "error", "exploring_failed")))
            return

        contexts = list(getattr(result, "output", []))
        selected = contexts[: max(1, int(msg.payload.get("max_listings", len(contexts)) or 1))]
        if self._on_fanout is not None:
            self._on_fanout(len(selected))
        for ctx in selected:
            context = JobApplicationContext.model_validate(ctx)
            if context.listing is not None:
                await self._repo.upsert_listing(context.listing, context.run_id)
            await self._repo.upsert_checkpoint(context.run_id, self.name, context.model_dump(mode="json"))
            self._maybe_export_dryrun_fixture(context, msg)

            child = Message(
                run_id=context.run_id,
                stage="qualification",
                payload=context.model_dump(mode="json"),
                reply_queue=PACKAGING_Q,
                dead_letter_queue=msg.dead_letter_queue,
                metadata=dict(msg.metadata),
            )
            await queue.enqueue(self._config.reply_queue, child)

            run_dir = self._artifacts_root / context.run_id
            run_dir.mkdir(parents=True, exist_ok=True)
            index_path = run_dir / "stage_outputs.md"
            if not index_path.exists():
                index_path.write_text(f"# Stage Outputs for {context.run_id}\\n\\n", encoding="utf-8")
            listing_json = context.listing.model_dump(mode="json") if context.listing is not None else {}
            listing_path = run_dir / self.name / "listing.json"
            listing_path.parent.mkdir(parents=True, exist_ok=True)
            listing_path.write_text(json.dumps(listing_json, indent=2, ensure_ascii=False) + "\\n", encoding="utf-8")
            with index_path.open("a", encoding="utf-8") as handle:
                handle.write(f"## Seeded at {datetime.now(timezone.utc).isoformat()}\\n")
                handle.write(f"- {self.name}: {self.name}/listing.json\\n")

        await queue.ack(self._config.input_queue, msg.message_id)

    async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = (ctx, attempt)

    def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = (ctx, attempt)
        print(f"[ok] exploring -> run_id={ctx.run_id}")
