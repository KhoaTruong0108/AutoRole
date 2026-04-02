from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from autorole.context import ExplorationSeed, JobApplicationContext
from autorole.queue import Message, QueueBackend, TAILORING_Q
from autorole.workers.base import StageWorker


class ExploringWorker(StageWorker):
    name = "exploring"

    def __init__(self, *args: object, on_fanout: Callable[[int], None] | None = None, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._on_fanout = on_fanout

    async def process(self, queue: QueueBackend, msg: Message) -> None:
        if hasattr(self._stage, "iter_source_listings") and hasattr(self._stage, "source_names"):
            await self._process_incremental(queue, msg)
            return

        result = await self._execute_inner(msg)
        if result is None:
            delay = self._backoff(msg.attempt)
            await queue.nack(self._config.input_queue, msg.message_id, delay)
            self._logger.error(
                "failed to process message stage=%s run_id=%s message_id=%s because=unhandled exception",
                self.name,
                msg.run_id,
                msg.message_id,
            )
            self._logger.exception("unhandled exception stage=%s run_id=%s", self.name, msg.run_id)
            return

        if not getattr(result, "success", False):
            await queue.enqueue(msg.dead_letter_queue, msg)
            await queue.ack(self._config.input_queue, msg.message_id)
            self._logger.warning(
                "failed to process message stage=%s run_id=%s message_id=%s because=%s",
                self.name,
                msg.run_id,
                msg.message_id,
                getattr(result, "error", "exploring_failed"),
            )
            self._logger.warning(
                "blocked stage=%s run_id=%s reason=%s",
                self.name,
                msg.run_id,
                getattr(result, "error", "exploring_failed"),
            )
            if self._on_block is not None:
                self._on_block(msg.run_id, str(getattr(result, "error", "exploring_failed")))
            return

        seeds = list(getattr(result, "output", []))
        selected = seeds[: max(1, int(msg.payload.get("max_listings", len(seeds)) or 1))]
        if self._on_fanout is not None:
            self._on_fanout(len(selected))
        for item in selected:
            seed = ExplorationSeed.model_validate(item)
            await self._emit_seed(queue, msg, seed)

        await queue.ack(self._config.input_queue, msg.message_id)

    async def _process_incremental(self, queue: QueueBackend, msg: Message) -> None:
        total_emitted = 0
        source_names = list(self._stage.source_names(msg))
        per_source_limit = max(1, int(msg.payload.get("max_listings", 1) or 1))

        try:
            async for source_name, listings in self._stage.iter_source_listings(msg):
                emitted_for_source = 0
                for listing in listings:
                    seed = ExplorationSeed(
                        listing=listing,
                        source_name=source_name,
                        discovered_at=datetime.now(timezone.utc),
                    )
                    await self._emit_seed(queue, msg, seed)
                    emitted_for_source += 1
                    total_emitted += 1
                    if emitted_for_source >= per_source_limit:
                        break

                if self._on_fanout is not None:
                    is_last_source = source_name == source_names[-1] if source_names else True
                    expected = total_emitted if is_last_source else total_emitted + 1
                    self._on_fanout(expected)
        except Exception:
            delay = self._backoff(msg.attempt)
            await queue.nack(self._config.input_queue, msg.message_id, delay)
            self._logger.error(
                "failed to process message stage=%s run_id=%s message_id=%s because=unhandled exception",
                self.name,
                msg.run_id,
                msg.message_id,
            )
            self._logger.exception("unhandled exception stage=%s run_id=%s", self.name, msg.run_id)
            return

        if total_emitted == 0:
            await queue.enqueue(msg.dead_letter_queue, msg)
            await queue.ack(self._config.input_queue, msg.message_id)
            reason = "No job listings found across all configured platforms"
            self._logger.warning(
                "failed to process message stage=%s run_id=%s message_id=%s because=%s",
                self.name,
                msg.run_id,
                msg.message_id,
                reason,
            )
            self._logger.warning("blocked stage=%s run_id=%s reason=%s", self.name, msg.run_id, reason)
            if self._on_block is not None:
                self._on_block(msg.run_id, reason)
            return

        await queue.ack(self._config.input_queue, msg.message_id)

    async def _emit_seed(self, queue: QueueBackend, msg: Message, seed: ExplorationSeed) -> None:
        transport_run_id = self._seed_transport_id(seed)
        child = Message(
            run_id=transport_run_id,
            stage="scoring",
            payload=seed.model_dump(mode="json"),
            reply_queue=TAILORING_Q,
            dead_letter_queue=msg.dead_letter_queue,
            metadata=dict(msg.metadata),
        )
        await queue.enqueue(self._config.reply_queue, child)

        run_dir = self._artifacts_root / transport_run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        index_path = run_dir / "stage_outputs.md"
        if not index_path.exists():
            index_path.write_text(f"# Stage Outputs for {transport_run_id}\\n\\n", encoding="utf-8")
        listing_json = seed.listing.model_dump(mode="json")
        listing_path = run_dir / self.name / "listing.json"
        listing_path.parent.mkdir(parents=True, exist_ok=True)
        listing_path.write_text(json.dumps(listing_json, indent=2, ensure_ascii=False) + "\\n", encoding="utf-8")
        with index_path.open("a", encoding="utf-8") as handle:
            handle.write(f"## Seeded at {datetime.now(timezone.utc).isoformat()}\\n")
            handle.write(f"- {self.name}: {self.name}/listing.json\\n")

    def _seed_transport_id(self, seed: ExplorationSeed) -> str:
        source = seed.source_name.lower().replace(" ", "_") or "source"
        return f"seed_{source}_{uuid4().hex[:12]}"

    async def on_success(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = (ctx, attempt)

    def log_ok(self, ctx: JobApplicationContext, attempt: int) -> None:
        _ = (ctx, attempt)
        print(f"[ok] exploring -> run_id={ctx.run_id}")
