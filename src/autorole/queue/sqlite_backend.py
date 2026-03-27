from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import aiosqlite

from autorole.queue.backend import Message, QueueBackend


class SqliteQueueBackend(QueueBackend):
    def __init__(self, db: aiosqlite.Connection) -> None:
        self._db = db

    async def enqueue(self, queue_name: str, message: Message) -> str:
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """
            INSERT INTO queue_messages (
                message_id,
                queue_name,
                run_id,
                stage,
                payload,
                attempt,
                reply_queue,
                dead_letter_queue,
                metadata,
                status,
                enqueued_at,
                visible_after
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (
                message.message_id,
                queue_name,
                message.run_id,
                message.stage,
                json.dumps(message.payload),
                message.attempt,
                message.reply_queue,
                message.dead_letter_queue,
                json.dumps(message.metadata),
                now,
                now,
            ),
        )
        await self._db.commit()
        return message.message_id

    async def pull(self, queue_name: str, visibility_timeout_seconds: int = 300) -> Message | None:
        now = datetime.now(timezone.utc)
        visible_after = (now + timedelta(seconds=visibility_timeout_seconds)).isoformat()
        now_iso = now.isoformat()

        await self._db.execute("BEGIN IMMEDIATE")
        try:
            async with self._db.execute(
                """
                SELECT
                    message_id,
                    run_id,
                    stage,
                    payload,
                    reply_queue,
                    dead_letter_queue,
                    attempt,
                    metadata
                FROM queue_messages
                WHERE queue_name = ?
                  AND status = 'pending'
                  AND visible_after <= ?
                ORDER BY enqueued_at ASC
                LIMIT 1
                """,
                (queue_name, now_iso),
            ) as cur:
                row = await cur.fetchone()

            if row is None:
                await self._db.rollback()
                return None

            message_id = str(row[0])
            await self._db.execute(
                """
                UPDATE queue_messages
                SET status = 'processing', visible_after = ?
                WHERE message_id = ?
                """,
                (visible_after, message_id),
            )
            await self._db.commit()
        except Exception:
            await self._db.rollback()
            raise

        return Message(
            message_id=str(row[0]),
            run_id=str(row[1]),
            stage=str(row[2]),
            payload=json.loads(row[3]),
            reply_queue=str(row[4]),
            dead_letter_queue=str(row[5]),
            attempt=int(row[6]),
            metadata=json.loads(row[7] or "{}"),
        )

    async def ack(self, queue_name: str, message_id: str) -> None:
        _ = queue_name
        await self._db.execute("DELETE FROM queue_messages WHERE message_id = ?", (message_id,))
        await self._db.commit()

    async def nack(self, queue_name: str, message_id: str, delay_seconds: int = 0) -> None:
        _ = queue_name
        visible_after = (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat()
        await self._db.execute(
            """
            UPDATE queue_messages
            SET status = 'pending', visible_after = ?
            WHERE message_id = ?
            """,
            (visible_after, message_id),
        )
        await self._db.commit()

    async def create_queue(self, queue_name: str) -> None:
        _ = queue_name
        return None
