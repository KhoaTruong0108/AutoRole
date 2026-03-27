from __future__ import annotations

import asyncio

from autorole.queue.backend import Message, QueueBackend


class InMemoryQueueBackend(QueueBackend):
    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[Message]] = {}
        self._in_flight: dict[str, Message] = {}

    async def enqueue(self, queue_name: str, message: Message) -> str:
        queue = self._queues.setdefault(queue_name, asyncio.Queue())
        queue.put_nowait(message)
        return message.message_id

    async def pull(self, queue_name: str, visibility_timeout_seconds: int = 300) -> Message | None:
        _ = visibility_timeout_seconds
        queue = self._queues.setdefault(queue_name, asyncio.Queue())
        try:
            message = queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        self._in_flight[message.message_id] = message
        return message

    async def ack(self, queue_name: str, message_id: str) -> None:
        _ = queue_name
        self._in_flight.pop(message_id, None)

    async def nack(self, queue_name: str, message_id: str, delay_seconds: int = 0) -> None:
        _ = delay_seconds
        queue = self._queues.setdefault(queue_name, asyncio.Queue())
        message = self._in_flight.pop(message_id, None)
        if message is None:
            return
        queue.put_nowait(message)

    async def create_queue(self, queue_name: str) -> None:
        self._queues.setdefault(queue_name, asyncio.Queue())
