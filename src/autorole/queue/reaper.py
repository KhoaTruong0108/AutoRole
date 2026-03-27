from __future__ import annotations

import asyncio

import aiosqlite


async def run_reaper(db: aiosqlite.Connection, interval_seconds: float = 30.0) -> None:
    while True:
        await db.execute(
            """
            UPDATE queue_messages
            SET status = 'pending', visible_after = datetime('now')
            WHERE status = 'processing'
              AND visible_after < datetime('now')
            """
        )
        await db.commit()
        await asyncio.sleep(interval_seconds)
