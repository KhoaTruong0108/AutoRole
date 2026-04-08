from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .._snapflow import Executor, StageResult, StateContext
from ..integrations.shared_browser import launch_shared_browser, shared_browser_ready, shared_browser_requested
from ..store import AutoRoleStoreAdapter


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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

        if platform in self._public_platforms:
            authenticated = False
            session_note = "public platform - no authentication required"
        else:
            authenticated = True
            session_note = f"authenticated via stored cookie for {platform}"

        session_payload = {
            "platform": platform,
            "authenticated": authenticated,
            "session_note": session_note,
            "established_at": _utcnow_iso(),
        }

        shared_browser: dict[str, Any]
        if shared_browser_requested(metadata, listing):
            try:
                shared_browser = await launch_shared_browser(
                    correlation_id=ctx.correlation_id,
                    metadata=metadata,
                    listing=listing,
                    authenticated=authenticated,
                )
            except Exception as exc:
                return StageResult.fail(
                    f"session failed to launch shared browser: {exc}",
                    "SessionPreconditionError",
                )

            if not shared_browser_ready(shared_browser):
                return StageResult.fail(
                    "session requires a ready shared browser but received an unready descriptor",
                    "SessionPreconditionError",
                )
        else:
            shared_browser = {
                "kind": "shared_browser",
                "status": "skipped",
                "reason": "synthetic_or_disabled",
                "skipped_at": _utcnow_iso(),
            }

        session_payload["shared_browser"] = shared_browser
        payload["session"] = session_payload
        payload["shared_browser"] = shared_browser

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
