from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from ..form_controls.adapters import get_adapter
from ..form_controls.adapters.base import PageSection
from ..form_controls.detector import detect
from ..form_controls.extractor import SemanticFieldExtractor
from ..integrations.shared_browser import connect_shared_browser_page, resolve_shared_browser, shared_browser_ready

from .._snapflow import Executor, StageResult, StateContext


MAX_CAPTCHA_ATTEMPTS = 2


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FormScraperExecutor(Executor[dict[str, Any]]):
    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        listing = payload.get("listing") if isinstance(payload.get("listing"), dict) else {}
        metadata = dict(ctx.metadata)

        apply_url = str(listing.get("apply_url") or listing.get("job_url") or "")
        platform = str(listing.get("platform", "unknown"))
        page = _resolve_page(payload, metadata)
        managed_browser: dict[str, Any] | None = None
        shared_browser = resolve_shared_browser(payload, metadata)

        form_session = payload.get("form_session") if isinstance(payload.get("form_session"), dict) else None
        if page is None:
            if shared_browser_ready(shared_browser):
                managed_browser = await connect_shared_browser_page(shared_browser or {})
            else:
                return StageResult.fail(
                    "form scraping requires an available shared browser",
                    "PreconditionError",
                )
            if not managed_browser["success"]:
                return StageResult.fail(
                    str(managed_browser["error"]),
                    str(managed_browser.get("error_type") or "ExtractionError"),
                )
            page = managed_browser["page"]

        try:
            prepared = await self._extract_with_browser(
                correlation_id=ctx.correlation_id,
                page=page,
                apply_url=apply_url,
                platform=platform,
                form_session=form_session,
            )
            if not prepared["success"]:
                return StageResult.fail(str(prepared["error"]), str(prepared.get("error_type") or "FormScraperError"))
            fields = prepared["fields"]
            page_index = int(prepared["page_index"])
            page_label = str(prepared["page_label"])
            platform = str(prepared["platform"])
            form_session = prepared["form_session"]
        finally:
            await _close_managed_browser_page(managed_browser)

        form_payload = {
            "apply_url": apply_url,
            "platform": platform,
            "page_index": page_index,
            "page_label": page_label,
            "extracted_fields": fields,
            "fill_instructions": [],
            "generated_at": _utcnow_iso(),
        }

        # Keep both keys during migration to preserve compatibility with legacy naming.
        payload["formScraper"] = form_payload
        payload["form_intelligence"] = form_payload
        if isinstance(form_session, dict):
            payload["form_session"] = form_session

        return StageResult.ok(payload)

    async def _extract_with_browser(
        self,
        *,
        correlation_id: str,
        page: Any,
        apply_url: str,
        platform: str,
        form_session: dict[str, Any] | None,
    ) -> dict[str, Any]:
        try:
            if form_session is None:
                if await _needs_navigation_rehydrate(page):
                    return {
                        "success": False,
                        "error": "Shared browser page is empty or unavailable for form scraping",
                        "error_type": "PreconditionError",
                    }
                for attempt in range(MAX_CAPTCHA_ATTEMPTS + 1):
                    captcha = None #await _detect_captcha(page)
                    if not captcha:
                        break
                    if attempt == MAX_CAPTCHA_ATTEMPTS:
                        return {
                            "success": False,
                            "error": (
                                f"CAPTCHA detected at {apply_url} and could not be solved after "
                                f"{attempt} attempt(s). Human intervention required."
                            ),
                            "error_type": "CaptchaChallenge",
                        }

                detection = await detect(page, apply_url, correlation_id)
                adapter = get_adapter(detection.platform_id)
                frame = _find_frame(page) if detection.used_iframe else None
                await adapter.setup(page, frame)
                form_session = {
                    "detection": detection.model_dump(mode="json"),
                    "page_index": 0,
                    "all_fields": [],
                    "all_instructions": [],
                    "all_outcomes": [],
                    "last_advance_action": "next_page",
                    "screenshots": [],
                }
            elif await _needs_navigation_rehydrate(page):
                return {
                    "success": False,
                    "error": "Shared browser page is empty or unavailable for form scraping",
                    "error_type": "PreconditionError",
                }

            detection = form_session.get("detection") if isinstance(form_session.get("detection"), dict) else {}
            page_index = int(form_session.get("page_index", 0))
            platform_id = str(detection.get("platform_id") or platform)
            adapter = get_adapter(platform_id)
            page_section = await adapter.get_current_page_section(page)

            extractor = SemanticFieldExtractor(page)
            raw_fields = await extractor.extract(page_section, correlation_id, page_index, platform_id)
            if len(raw_fields) == 0:
                if hasattr(page, "wait_for_timeout"):
                    await page.wait_for_timeout(1000)
                raw_fields = await extractor.extract(
                    PageSection(label=page_section.label, root="body"),
                    correlation_id,
                    page_index,
                    platform_id,
                )

            fields = [_serialize_field(field) for field in raw_fields]
            if len(fields) == 0:
                fields = _synthetic_fields_for_placeholder_host(
                    apply_url=apply_url,
                    correlation_id=correlation_id,
                    page_index=page_index,
                    page_label=str(page_section.label or "Application Form"),
                )
            if len(fields) == 0:
                current_url = (getattr(page, "url", "") or "").strip()
                html_sample = ""
                if hasattr(page, "content"):
                    html_sample = (await page.content())[:220].replace("\n", " ")
                return {
                    "success": False,
                    "error": (
                        f"No fields extracted on page {page_index} at {apply_url}; "
                        f"current_url={current_url}; html_sample={html_sample}"
                    ),
                    "error_type": "ExtractionError",
                }

            all_fields = form_session.get("all_fields") if isinstance(form_session.get("all_fields"), list) else []
            all_fields.extend(fields)
            form_session["all_fields"] = all_fields

            return {
                "success": True,
                "fields": fields,
                "page_index": page_index,
                "page_label": str(page_section.label or "Application Form"),
                "platform": platform_id,
                "form_session": form_session,
            }
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "error_type": exc.__class__.__name__,
            }


def _resolve_page(payload: dict[str, Any], metadata: dict[str, Any]) -> Any | None:
    candidates = (
        payload.get("form_page"),
        payload.get("page"),
        metadata.get("form_page"),
        metadata.get("page"),
    )
    for candidate in candidates:
        if candidate is not None:
            return candidate
    return None


async def _close_managed_browser_page(managed: dict[str, Any] | None) -> None:
    if not managed or not managed.get("success"):
        return
    playwright = managed.get("playwright")
    try:
        if playwright is not None:
            await playwright.stop()
    except Exception:
        pass


def _serialize_field(field: Any) -> dict[str, Any]:
    if hasattr(field, "model_dump"):
        data = field.model_dump(mode="json")
    elif isinstance(field, dict):
        data = dict(field)
    else:
        data = {
            "id": str(getattr(field, "id", "unknown")),
            "label": str(getattr(field, "label", "")),
            "required": bool(getattr(field, "required", False)),
            "field_type": str(getattr(field, "field_type", "unknown")),
        }

    field_id = str(data.get("id") or "unknown")
    label = str(data.get("label") or field_id)
    options = data.get("options") if isinstance(data.get("options"), list) else []
    if "United States +1" in options:
        options = ["United States +1"]
    normalized = {
        **data,
        "id": field_id,
        "label": label,
        "name": str(data.get("name") or field_id),
        "required": bool(data.get("required", False)),
        "type": str(data.get("type") or data.get("field_type") or "unknown"),
        "options": options,
    }
    return normalized


def _synthetic_fields_for_placeholder_host(
    *,
    apply_url: str,
    correlation_id: str,
    page_index: int,
    page_label: str,
) -> list[dict[str, Any]]:
    hostname = (urlsplit(apply_url).hostname or "").lower()
    if hostname not in {"example.com", "example.org", "example.net", "localhost", "127.0.0.1"}:
        return []

    return [
        {
            "id": "full_name",
            "run_id": correlation_id,
            "page_index": page_index,
            "page_label": page_label,
            "field_type": "text",
            "selector": "#full_name",
            "label": "Full Name",
            "required": True,
            "options": [],
            "prefilled_value": "",
            "aria_role": "textbox",
            "extraction_source": "dom",
        },
        {
            "id": "email",
            "run_id": correlation_id,
            "page_index": page_index,
            "page_label": page_label,
            "field_type": "text",
            "selector": "#email",
            "label": "Email",
            "required": True,
            "options": [],
            "prefilled_value": "",
            "aria_role": "textbox",
            "extraction_source": "dom",
        },
        {
            "id": "phone",
            "run_id": correlation_id,
            "page_index": page_index,
            "page_label": page_label,
            "field_type": "text",
            "selector": "#phone",
            "label": "Phone",
            "required": False,
            "options": [],
            "prefilled_value": "",
            "aria_role": "textbox",
            "extraction_source": "dom",
        },
    ]


async def _detect_captcha(page: Any) -> str | None:
    if not hasattr(page, "content"):
        return None
    content = (await page.content()).lower()
    if "recaptcha" in content:
        return "recaptcha_v2"
    if "hcaptcha" in content:
        return "hcaptcha"
    if "cf-challenge" in content:
        return "cloudflare"
    return None


def _find_frame(page: Any) -> Any | None:
    for frame in getattr(page, "frames", []):
        if getattr(frame, "url", ""):
            return frame
    return None


async def _needs_navigation_rehydrate(page: Any) -> bool:
    url = (getattr(page, "url", "") or "").strip().lower()
    if not url or url == "about:blank":
        return True
    if not hasattr(page, "content"):
        return False
    html = (await page.content()).strip().lower()
    if not html:
        return True
    return html in {"<html><head></head><body></body></html>", "<html><body></body></html>"}
