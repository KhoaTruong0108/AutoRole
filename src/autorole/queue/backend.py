from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

EXPLORING_Q = "exploring_q"
SCORING_Q = "scoring_q"
PACKAGING_Q = "packaging_q"
SESSION_Q = "session_q"
FORM_INTEL_Q = "form_intel_q"
LLM_FIELD_COMPLETER_Q = "llm_field_completer_q"
FORM_SUB_Q = "form_sub_q"
CONCLUDING_Q = "concluding_q"
DEAD_LETTER_Q = "dead_letter_q"


@dataclass
class Message:
    run_id: str
    stage: str
    payload: dict[str, Any]
    reply_queue: str
    dead_letter_queue: str
    message_id: str = field(default_factory=lambda: str(uuid4()))
    attempt: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)


class QueueBackend(ABC):
    @abstractmethod
    async def enqueue(self, queue_name: str, message: Message) -> str:
        ...

    @abstractmethod
    async def pull(self, queue_name: str, visibility_timeout_seconds: int = 300) -> Message | None:
        ...

    @abstractmethod
    async def ack(self, queue_name: str, message_id: str) -> None:
        ...

    @abstractmethod
    async def nack(self, queue_name: str, message_id: str, delay_seconds: int = 0) -> None:
        ...

    @abstractmethod
    async def create_queue(self, queue_name: str) -> None:
        ...
