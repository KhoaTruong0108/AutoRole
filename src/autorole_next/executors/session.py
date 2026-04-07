from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
from typing import Any

from .._snapflow import Executor, StageResult, StateContext
from ..store import AutoRoleStoreAdapter


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_debug_sleep_seconds(metadata: dict[str, Any]) -> float:
    raw_value = metadata.get("debug_session_sleep_seconds")
    if raw_value in (None, ""):
        raw_value = os.environ.get("AR_DEBUG_SESSION_SLEEP_SECONDS")
    if raw_value in (None, ""):
        return 0.0
    try:
        delay_seconds = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid debug_session_sleep_seconds: {raw_value}") from exc
    if delay_seconds < 0:
        raise ValueError(f"debug_session_sleep_seconds must be non-negative, got {raw_value}")
    return delay_seconds


class SessionExecutor(Executor[dict[str, Any]]):
    _store: AutoRoleStoreAdapter | None = None
    _public_platforms = {"ashby", "greenhouse", "lever", "workday"}

    @classmethod
    def configure_store(cls, store: AutoRoleStoreAdapter) -> None:
        cls._store = store

    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        metadata = dict(ctx.metadata)
        listing = payload.get("listing") if isinstance(payload.get("listing"), dict) else {}
        platform = str(listing.get("platform", "")).lower().strip() or "unknown"
        debug_sleep_seconds = 300 #_resolve_debug_sleep_seconds(metadata)

        if platform in self._public_platforms:
            authenticated = False
            session_note = "public platform - no authentication required"
        else:
            authenticated = True
            session_note = f"authenticated via stored cookie for {platform}"

        if debug_sleep_seconds > 0:
            await asyncio.sleep(debug_sleep_seconds)
            session_note = f"{session_note}; debug sleep injected ({debug_sleep_seconds:.3f}s)"

        session_payload = {
            "platform": platform,
            "authenticated": authenticated,
            "session_note": session_note,
            "established_at": _utcnow_iso(),
        }
        if debug_sleep_seconds > 0:
            session_payload["debug_sleep_seconds"] = debug_sleep_seconds
        payload["session"] = session_payload

        store = self._store
        if store is None:
            raise RuntimeError("SessionExecutor store is not configured")

        await store.upsert_session(
            ctx.correlation_id,
            platform=platform,
            authenticated=authenticated,
            session_note=session_note,
        )

        return StageResult.ok(payload)
