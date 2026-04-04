from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._snapflow import Executor, StageResult, StateContext
from ..store import AutoRoleStoreAdapter


class PackagingExecutor(Executor[dict[str, Any]]):
    _store: AutoRoleStoreAdapter | None = None

    @classmethod
    def configure_store(cls, store: AutoRoleStoreAdapter) -> None:
        cls._store = store

    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        tailoring = payload.get("tailoring") if isinstance(payload.get("tailoring"), dict) else {}
        resume_path = str(tailoring.get("resume_path", f"resumes/{ctx.correlation_id}/tailored.md"))
        pdf_path = str(Path(resume_path).with_suffix(".pdf"))
        self._materialize_packaged_pdf(pdf_path, correlation_id=ctx.correlation_id)

        packaging_payload = {
            "resume_path": resume_path,
            "pdf_path": pdf_path,
            "packaged_at": datetime.now(timezone.utc).isoformat(),
            "status": "ready",
        }
        payload["packaging"] = packaging_payload
        payload["packaged"] = packaging_payload

        store = self._store
        if store is None:
            raise RuntimeError("PackagingExecutor store is not configured")

        await store.upsert_application_packaging(
            ctx.correlation_id,
            resume_path=resume_path,
            pdf_path=pdf_path,
        )
        return StageResult.ok(payload)

    @staticmethod
    def _materialize_packaged_pdf(pdf_path: str, *, correlation_id: str) -> None:
        target = Path(pdf_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return
        pdf_bytes = (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]/Contents 4 0 R/Resources<<>>>>endobj\n"
            b"4 0 obj<</Length 61>>stream\n"
            b"BT /F1 12 Tf 24 96 Td (Packaged resume: "
            + correlation_id.encode("ascii", errors="ignore")
            + b") Tj ET\n"
            b"endstream endobj\n"
            b"xref\n0 5\n0000000000 65535 f \n0000000009 00000 n \n0000000056 00000 n \n"
            b"0000000113 00000 n \n0000000215 00000 n \n"
            b"trailer<</Size 5/Root 1 0 R>>\nstartxref\n331\n%%EOF\n"
        )
        target.write_bytes(pdf_bytes)
