from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from autorole_next.form_controls.adapters import get_adapter
from autorole_next.form_controls.detector import detect
from autorole_next.form_controls.dom_appliers import AsyncDOMFormApplier
from autorole_next.form_controls.extractor import SemanticFieldExtractor
from autorole_next.form_controls.models import ExtractedField


class ExternalPackageFormExtractor:
    """URL-driven extractor backed by autorole_next form controls."""

    async def extract(self, page: Any) -> dict[str, Any]:
        _ = page
        raise NotImplementedError(
            "ExternalPackageFormExtractor requires URL-driven invocation and is not page-bound. "
            "Use scrape_url(...) instead."
        )

    async def scrape_url(self, url: str, *, headless: bool = True, timeout_ms: int = 60000) -> dict[str, Any]:
        browser = None
        context = None
        playwright = None
        try:
            from playwright.async_api import async_playwright

            run_id = f"external-{uuid.uuid4()}"
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=headless)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            detection = await detect(page, url, run_id)
            adapter = get_adapter(detection.platform_id)
            await adapter.setup(page)
            section = await adapter.get_current_page_section(page)
            fields = await SemanticFieldExtractor(page).extract(section, run_id, 0, detection.platform_id)
            return {
                "success": True,
                "run_id": run_id,
                "platform": detection.platform_id,
                "apply_url": str(getattr(page, "url", "") or url),
                "page_index": 0,
                "page_label": str(section.label or "Application Form"),
                "extracted_fields": [field.model_dump(mode="json") for field in fields],
                "detection": detection.model_dump(mode="json"),
            }
        finally:
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()
            if playwright is not None:
                await playwright.stop()


class ExternalPackageFormApplier:
    """URL-driven applier backed by autorole_next form controls."""

    async def fill(self, page: Any, form_json_filled: dict[str, Any]) -> None:
        _ = (page, form_json_filled)
        raise NotImplementedError(
            "ExternalPackageFormApplier requires URL-driven invocation and does not support page-bound fill()."
        )

    async def attach_resume(self, page: Any, file_path: str) -> None:
        _ = (page, file_path)
        raise NotImplementedError(
            "ExternalPackageFormApplier requires URL-driven invocation and does not support page-bound attach_resume()."
        )

    async def submit(self, page: Any) -> None:
        _ = page
        raise NotImplementedError(
            "ExternalPackageFormApplier requires URL-driven invocation and does not support page-bound submit()."
        )

    async def confirm(self, page: Any) -> bool:
        _ = page
        raise NotImplementedError(
            "ExternalPackageFormApplier requires URL-driven invocation and does not support page-bound confirm()."
        )

    async def apply_url(
        self,
        url: str,
        blueprint: dict[str, Any],
        *,
        headless: bool = True,
        timeout_ms: int = 60000,
        output_path: str | None = None,
    ) -> dict[str, Any]:
        browser = None
        context = None
        playwright = None
        try:
            from playwright.async_api import async_playwright

            run_id = f"external-{uuid.uuid4()}"
            playwright = await async_playwright().start()
            browser = await playwright.chromium.launch(headless=headless)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

            detection = await detect(page, url, run_id)
            adapter = get_adapter(detection.platform_id)
            await adapter.setup(page)

            form_payload = _normalize_blueprint(blueprint, run_id)
            applier = AsyncDOMFormApplier()
            await applier.fill(page, form_payload)
            resume_path = str(blueprint.get("resume_path") or blueprint.get("pdf_path") or "")
            if resume_path:
                await applier.attach_resume(page, resume_path)

            submitted = bool(blueprint.get("submit", True))
            confirmed = False
            if submitted:
                await applier.submit(page)
                confirmed = await applier.confirm(page)

            result = {
                "success": True,
                "run_id": run_id,
                "platform": detection.platform_id,
                "apply_url": str(getattr(page, "url", "") or url),
                "submitted": submitted,
                "confirmed": confirmed,
            }
        finally:
            if context is not None:
                await context.close()
            if browser is not None:
                await browser.close()
            if playwright is not None:
                await playwright.stop()
        if output_path:
            Path(output_path).write_text(str(result), encoding="utf-8")
        return result


def _normalize_blueprint(blueprint: dict[str, Any], run_id: str) -> dict[str, Any]:
    fields = blueprint.get("fields")
    if isinstance(fields, list):
        normalized_fields: list[dict[str, Any]] = []
        for item in fields:
            if not isinstance(item, dict):
                continue
            field_id = str(item.get("id") or item.get("field_id") or "").strip()
            if not field_id:
                continue
            field_type = str(item.get("type") or item.get("field_type") or "text")
            value = item.get("value")
            normalized_fields.append({"id": field_id, "type": field_type, "value": value})
        return {"fields": normalized_fields}

    instructions = blueprint.get("fill_instructions")
    extracted = blueprint.get("extracted_fields")
    if isinstance(instructions, list) and isinstance(extracted, list):
        extracted_by_id: dict[str, ExtractedField] = {}
        for field in extracted:
            if not isinstance(field, dict):
                continue
            try:
                model = ExtractedField.model_validate(field)
            except Exception:
                continue
            extracted_by_id[model.id] = model

        normalized_fields = []
        for item in instructions:
            if not isinstance(item, dict):
                continue
            field_id = str(item.get("field_id") or "").strip()
            if not field_id:
                continue
            source_field = extracted_by_id.get(field_id)
            normalized_fields.append(
                {
                    "id": field_id,
                    "type": source_field.field_type if source_field is not None else "text",
                    "value": item.get("value"),
                }
            )
        return {"fields": normalized_fields}

    return {"fields": [], "run_id": run_id}
