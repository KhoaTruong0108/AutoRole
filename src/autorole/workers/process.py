from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

from autorole.queue import QueueBackend
from autorole.workers.base import StageWorker


class StageWorkerProcess:
    def __init__(
        self,
        workers: Sequence[StageWorker],
        queue: QueueBackend,
        *,
        headless: bool = True,
    ) -> None:
        self._workers = list(workers)
        self._queue = queue
        self._headless = headless

    async def run(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except Exception:
            tasks = [asyncio.create_task(w.run_forever(self._queue)) for w in self._workers]
            await asyncio.gather(*tasks)
            return

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=self._headless)
            context = await browser.new_context()

            pages: list[Any] = []
            for _ in self._workers:
                pages.append(await context.new_page())

            for worker, page in zip(self._workers, pages):
                stage_page = getattr(worker._stage, "_page", None)
                if stage_page is None and hasattr(worker._stage, "_scoring"):
                    if getattr(worker._stage._scoring, "_page", None) is None:
                        worker._stage._scoring._page = page
                elif stage_page is None:
                    setattr(worker._stage, "_page", page)

            tasks = [asyncio.create_task(w.run_forever(self._queue)) for w in self._workers]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await context.close()
                await browser.close()
