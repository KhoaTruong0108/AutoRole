from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autorole_next.config import AppConfig
from autorole_next.integrations.renderer import ResumeRenderer, build_renderer
from autorole_next.tailoring_engine import build_packaged_pdf_path

from .._snapflow import Executor, StageResult, StateContext
from ..store import AutoRoleStoreAdapter


class PackagingExecutor(Executor[dict[str, Any]]):
    _store: AutoRoleStoreAdapter | None = None
    _renderer: ResumeRenderer | None = None

    @classmethod
    def configure_store(cls, store: AutoRoleStoreAdapter) -> None:
        cls._store = store

    @classmethod
    def configure_renderer(cls, renderer: ResumeRenderer | None) -> None:
        cls._renderer = renderer

    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        tailoring = payload.get("tailoring") if isinstance(payload.get("tailoring"), dict) else None
        if tailoring is None and isinstance(payload.get("tailored"), dict):
            tailoring = dict(payload.get("tailored"))
        if tailoring is None:
            return StageResult.fail("PackagingExecutor: tailoring payload is required", "PreconditionError")

        resume_path = str(tailoring.get("resume_path") or "").strip()
        if not resume_path:
            return StageResult.fail("PackagingExecutor: tailoring.resume_path is required", "PreconditionError")

        md_path = Path(resume_path).expanduser()
        if not md_path.exists() or not md_path.is_file():
            return StageResult.fail(f"PackagingExecutor: tailored resume not found at {md_path}", "PreconditionError")

        markdown_text = md_path.read_text(encoding="utf-8").strip()
        if not markdown_text:
            return StageResult.fail("PackagingExecutor: tailored resume is empty", "PreconditionError")

        pdf_path = build_packaged_pdf_path(str(md_path))
        renderer = self._renderer or build_renderer(AppConfig().renderer)
        try:
            await renderer.render(md_path, Path(pdf_path))
        except Exception as exc:
            return StageResult.fail(f"PDF rendering failed: {exc}", "RenderError")

        pdf_target = Path(pdf_path)
        if not pdf_target.exists() or pdf_target.stat().st_size == 0:
            return StageResult.fail("PackagingExecutor: rendered PDF is empty", "RenderError")

        packaging_payload = {
            "resume_id": str(tailoring.get("resume_id") or ""),
            "resume_path": resume_path,
            "pdf_path": pdf_path,
            "packaged_at": datetime.now(timezone.utc).isoformat(),
            "status": "ready",
        }
        payload["packaging"] = packaging_payload

        store = self._store
        if store is None:
            raise RuntimeError("PackagingExecutor store is not configured")

        await store.upsert_application_packaging(
            ctx.correlation_id,
            resume_path=resume_path,
            pdf_path=pdf_path,
        )
        return StageResult.ok(payload)
