from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..form_controls.adapters import get_adapter
from ..form_controls.executor import FormExecutor, _build_audit_log, _write_audit_log
from ..form_controls.models import DetectionResult, ExecutionResult, ExtractedField, FieldOutcome, FillInstruction
from ..integrations.shared_browser import connect_shared_browser_page, resolve_shared_browser, shared_browser_ready
from .._snapflow import Executor, StageResult, StateContext
from ..store import AutoRoleStoreAdapter


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class FormSubmissionExecutor(Executor[dict[str, Any]]):
    _store: AutoRoleStoreAdapter | None = None

    def __init__(self, executor: FormExecutor | None = None) -> None:
        self._executor = executor or FormExecutor()

    @classmethod
    def configure_store(cls, store: AutoRoleStoreAdapter) -> None:
        cls._store = store

    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        metadata = dict(ctx.metadata)
        previous_submission = payload.get("form_submission") if isinstance(payload.get("form_submission"), dict) else {}
        prior_loop_count = int(previous_submission.get("loop_count", 0))

        listing = payload.get("listing") if isinstance(payload.get("listing"), dict) else None
        form_payload = payload.get("formScraper") if isinstance(payload.get("formScraper"), dict) else None
        if form_payload is None and isinstance(payload.get("form_intelligence"), dict):
            form_payload = dict(payload.get("form_intelligence"))
        completion_payload = payload.get("fieldCompleter") if isinstance(payload.get("fieldCompleter"), dict) else None
        if completion_payload is None and isinstance(payload.get("llm_field_completer"), dict):
            completion_payload = dict(payload.get("llm_field_completer"))
        form_session = payload.get("form_session") if isinstance(payload.get("form_session"), dict) else None
        packaged = payload.get("packaging") if isinstance(payload.get("packaging"), dict) else None
        if packaged is None and isinstance(payload.get("packaged"), dict):
            packaged = dict(payload.get("packaged"))

        if (
            listing is None
            or form_payload is None
            or completion_payload is None
            or form_session is None
            or packaged is None
        ):
            return StageResult.fail(
                (
                    "FormSubmissionExecutor: listing, form payload, completion payload, "
                    "form_session and packaging must be set"
                ),
                "PreconditionError",
            )

        extracted_fields_raw = (
            form_payload.get("extracted_fields") if isinstance(form_payload.get("extracted_fields"), list) else None
        )
        instructions_raw = (
            completion_payload.get("fill_instructions")
            if isinstance(completion_payload.get("fill_instructions"), list)
            else form_payload.get("fill_instructions")
            if isinstance(form_payload.get("fill_instructions"), list)
            else None
        )
        if extracted_fields_raw is None or instructions_raw is None:
            return StageResult.fail(
                "FormSubmissionExecutor: extracted fields and fill instructions must be set",
                "PreconditionError",
            )

        page_index = int(form_payload.get("page_index", form_session.get("page_index", 0)))
        page_label = str(form_payload.get("page_label") or f"page_{page_index}")
        platform_id = _platform_id(form_session, listing)
        apply_url = str(listing.get("apply_url") or listing.get("job_url") or "")

        try:
            fields = [
                ExtractedField.model_validate(
                    _coerce_extracted_field_payload(
                        field,
                        run_id=ctx.correlation_id,
                        page_index=page_index,
                        page_label=page_label,
                    )
                )
                for field in extracted_fields_raw
            ]
            instructions = [
                FillInstruction.model_validate(
                    _coerce_fill_instruction_payload(inst, run_id=ctx.correlation_id, page_index=page_index)
                )
                for inst in instructions_raw
            ]
        except Exception as exc:
            return StageResult.fail(f"Invalid form payload for submission: {exc}", "PreconditionError")

        apply_mode = str(metadata.get("apply_mode", "")).lower()
        force_loop = bool(metadata.get("force_form_loop", False))
        guardrail_block = bool(metadata.get("submit_disabled", False))

        managed_browser: dict[str, Any] | None = None
        page = _resolve_page(payload, metadata)
        shared_browser = resolve_shared_browser(payload, metadata)
        if page is None:
            if shared_browser_ready(shared_browser):
                managed_browser = await connect_shared_browser_page(shared_browser or {})
            else:
                return StageResult.fail(
                    "form submission requires an available shared browser",
                    "PreconditionError",
                )
            if not managed_browser.get("success", False):
                return StageResult.fail(
                    str(managed_browser.get("error") or "form submission requires a browser page"),
                    str(managed_browser.get("error_type") or "SubmissionError"),
                )
            page = managed_browser.get("page")

        try:
            outcomes: list[FieldOutcome] = []
            screenshot_path = ""
            action = "submit" 
            applied_payload: dict[str, Any] | None = None
            if page is not None:
                adapter = get_adapter(platform_id)
                try:
                    ready = await _ensure_submission_page_ready(
                        page=page,
                        adapter=adapter,
                        apply_url=apply_url,
                        detection=form_session.get("detection") if isinstance(form_session.get("detection"), dict) else {},
                    )
                    if not ready["success"]:
                        return StageResult.fail(str(ready["error"]), str(ready.get("error_type") or "SubmissionError"))

                    outcomes = await self._executor.execute_page(page, fields, instructions, run_id=ctx.correlation_id)

                    field_map = {field.id: field for field in fields}
                    required_failures = [
                        outcome
                        for outcome in outcomes
                        if outcome.status in {"fill_error", "selector_not_found"}
                        and outcome.field_id in field_map
                        and bool(field_map[outcome.field_id].required)
                    ]

                    file_input = await adapter.get_file_input(page)
                    packaged_pdf_path = str(packaged.get("pdf_path") or "")
                    if file_input is not None and packaged_pdf_path:
                        await file_input.set_input_files(packaged_pdf_path)
                        if hasattr(page, "wait_for_timeout"):
                            await page.wait_for_timeout(500)

                    artifacts_dir = Path("logs") / "form_submission" / ctx.correlation_id
                    artifacts_dir.mkdir(parents=True, exist_ok=True)
                    page_section_label = page_label.replace(" ", "_")[:40]
                    screenshot_path = str(artifacts_dir / f"page_{page_index}_{page_section_label}.png")
                    if hasattr(page, "screenshot"):
                        await page.screenshot(path=screenshot_path)

                    if required_failures:
                        failed_ids = ", ".join(outcome.field_id for outcome in required_failures)
                        return StageResult.fail(
                            f"Required field(s) could not be filled; failing_field_ids=[{failed_ids}]",
                            "RequiredFieldFillError",
                        )

                    if guardrail_block:
                        action = "guardrail_block"
                        audit_path = self._write_audit_log(
                            correlation_id=ctx.correlation_id,
                            payload={
                                "action": action,
                                "reason": "submission disabled by operator guardrail",
                                "apply_mode": apply_mode,
                                "platform_id": platform_id,
                                "outcomes": [item.model_dump(mode="json") for item in outcomes],
                                "timestamp": _utcnow_iso(),
                            },
                        )
                    else:
                        if force_loop and prior_loop_count == 0:
                            action = "next_page"
                        else:
                            action = await adapter.advance(page)

                        if action == "submit":
                            post_submit = str(artifacts_dir / "post_submit.png")
                            if hasattr(page, "screenshot"):
                                await page.screenshot(path=post_submit)

                            success = await adapter.confirm_success(page)
                            if not success:
                                errors: list[str] = []
                                if hasattr(page, "locator"):
                                    try:
                                        errors = await page.locator('[class*="error"], [role="alert"]').all_text_contents()
                                    except Exception:
                                        errors = []
                                return StageResult.fail(
                                    f"Submission not confirmed. Page errors: {errors}",
                                    "SubmissionError",
                                )

                            confirmation_text = ""
                            if hasattr(page, "locator"):
                                try:
                                    confirmation_text = (await page.locator("body").inner_text())[:500]
                                except Exception:
                                    confirmation_text = ""

                            all_outcomes = _coerce_outcomes(form_session.get("all_outcomes")) + outcomes
                            detection = _coerce_detection_result(form_session.get("detection"), ctx.correlation_id, apply_url, platform_id)
                            execution_result = ExecutionResult(
                                run_id=ctx.correlation_id,
                                success=True,
                                platform_id=platform_id,
                                apply_url=apply_url,
                                submitted_at=_utcnow_iso(),
                                confirmation_text=confirmation_text,
                                field_outcomes=all_outcomes,
                                screenshot_pre=screenshot_path,
                                screenshot_post=post_submit,
                                error=None,
                            )

                            audit = _build_audit_log(
                                run_id=ctx.correlation_id,
                                started_at=_started_at_iso(payload),
                                job_url=str(listing.get("job_url") or ""),
                                detection=detection,
                                all_fields=_coerce_fields(form_session.get("all_fields")) + fields,
                                all_instructions=_coerce_instructions(form_session.get("all_instructions")) + instructions,
                                all_outcomes=all_outcomes,
                                result=execution_result,
                            )
                            audit_path = _write_audit_log(audit, ctx.correlation_id)

                            applied_payload = {
                                "resume_id": str(packaged.get("resume_id") or ""),
                                "execution_result": execution_result.model_dump(mode="json"),
                                "audit_log_path": audit_path,
                                "applied_at": _utcnow_iso(),
                                "submission_status": "submitted",
                                "submission_confirmed": True,
                            }
                        else:
                            audit_path = self._write_audit_log(
                                correlation_id=ctx.correlation_id,
                                payload={
                                    "action": action,
                                    "platform_id": platform_id,
                                    "outcomes": [item.model_dump(mode="json") for item in outcomes],
                                    "timestamp": _utcnow_iso(),
                                },
                            )
                except Exception as exc:
                    if _is_target_closed_error(exc):
                        return StageResult.fail(
                            "Submission page was closed unexpectedly during form submission execution",
                            "TargetClosedError",
                        )
                    raise
            else:
                audit_path = self._write_audit_log(
                    correlation_id=ctx.correlation_id,
                    payload={
                        "action": "done",
                        "reason": "dry-run submission simulated (no browser page)",
                        "timestamp": _utcnow_iso(),
                    },
                )

            if guardrail_block:
                decision = "block"
                reason = "submission disabled by operator guardrail"
                status = "submit_disabled"
                confirmed = False
                loop_count = prior_loop_count
            elif action == "submit":
                decision = "pass"
                reason = "submission completed"
                status = "submitted"
                confirmed = True
                loop_count = prior_loop_count
            elif action == "next_page":
                decision = "loop"
                reason = "additional scrape cycle requested"
                status = "rescrape_required"
                confirmed = False
                loop_count = prior_loop_count + 1
            else:
                decision = "pass"
                reason = "dry-run submission simulated"
                status = "dry_run"
                confirmed = False
                loop_count = prior_loop_count

            all_outcomes_session = form_session.get("all_outcomes") if isinstance(form_session.get("all_outcomes"), list) else []
            all_outcomes_session.extend([outcome.model_dump(mode="json") for outcome in outcomes])
            form_session["all_outcomes"] = all_outcomes_session
            screenshots = form_session.get("screenshots") if isinstance(form_session.get("screenshots"), list) else []
            if screenshot_path:
                screenshots.append(screenshot_path)
            form_session["screenshots"] = screenshots
            form_session["last_advance_action"] = action
            if action in {"next_page", "submit"}:
                form_session["page_index"] = int(form_session.get("page_index", 0)) + 1

            submitted_at = _utcnow_iso()
            submission_payload = {
                "decision": decision,
                "reason": reason,
                "status": status,
                "confirmed": confirmed,
                "loop_count": int(loop_count),
                "audit_log_path": audit_path,
                "submitted_at": submitted_at,
            }
            payload["form_submission"] = submission_payload
            payload["form_session"] = form_session
            if applied_payload is not None:
                payload["applied"] = applied_payload

            store = self._store
            if store is None:
                raise RuntimeError("FormSubmissionExecutor store is not configured")

            await store.upsert_application_submission(
                ctx.correlation_id,
                status=status,
                confirmed=bool(confirmed),
                applied_at=submitted_at,
            )

            return StageResult.ok(payload)
        finally:
            await _close_managed_browser_page(managed_browser)

    @staticmethod
    def _write_audit_log(*, correlation_id: str, payload: dict[str, Any]) -> str:
        path = Path("logs") / "form_submission" / correlation_id / "audit.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return str(path)


def _coerce_extracted_field_payload(
    field: Any,
    *,
    run_id: str,
    page_index: int,
    page_label: str,
) -> dict[str, Any]:
    raw = dict(field) if isinstance(field, dict) else {}
    field_id = str(raw.get("id") or raw.get("name") or "unknown")
    selector = str(raw.get("selector") or f"[id='{field_id}']")
    field_type = str(raw.get("field_type") or raw.get("type") or "unknown").lower()
    if field_type not in {
        "text",
        "textarea",
        "select",
        "radio",
        "checkbox",
        "combobox_search",
        "combobox_lazy",
        "date",
        "file",
        "hidden",
        "unknown",
    }:
        field_type = "text"

    return {
        "id": field_id,
        "run_id": str(raw.get("run_id") or run_id),
        "page_index": int(raw.get("page_index", page_index)),
        "page_label": str(raw.get("page_label") or page_label),
        "field_type": field_type,
        "selector": selector,
        "label": str(raw.get("label") or field_id),
        "required": bool(raw.get("required", False)),
        "options": raw.get("options") if isinstance(raw.get("options"), list) else [],
        "prefilled_value": str(raw.get("prefilled_value") or ""),
        "aria_role": str(raw.get("aria_role") or ""),
        "extraction_source": str(raw.get("extraction_source") or "dom"),
    }


def _coerce_fill_instruction_payload(
    instruction: Any,
    *,
    run_id: str,
    page_index: int,
) -> dict[str, Any]:
    raw = dict(instruction) if isinstance(instruction, dict) else {}
    field_id = str(raw.get("field_id") or raw.get("id") or "unknown")
    action = str(raw.get("action") or "skip")
    if action not in {"fill", "skip", "human_review"}:
        action = "skip"
    return {
        "field_id": field_id,
        "run_id": str(raw.get("run_id") or run_id),
        "action": action,
        "value": raw.get("value"),
        "source": str(raw.get("source") or "generated"),
        "page_index": int(raw.get("page_index", page_index)),
    }


def _coerce_fields(raw_fields: Any) -> list[ExtractedField]:
    if not isinstance(raw_fields, list):
        return []
    fields: list[ExtractedField] = []
    for item in raw_fields:
        try:
            if isinstance(item, ExtractedField):
                fields.append(item)
            elif isinstance(item, dict):
                fields.append(ExtractedField.model_validate(item))
        except Exception:
            continue
    return fields


def _coerce_instructions(raw_instructions: Any) -> list[FillInstruction]:
    if not isinstance(raw_instructions, list):
        return []
    instructions: list[FillInstruction] = []
    for item in raw_instructions:
        try:
            if isinstance(item, FillInstruction):
                instructions.append(item)
            elif isinstance(item, dict):
                instructions.append(FillInstruction.model_validate(item))
        except Exception:
            continue
    return instructions


def _coerce_outcomes(raw_outcomes: Any) -> list[FieldOutcome]:
    if not isinstance(raw_outcomes, list):
        return []
    outcomes: list[FieldOutcome] = []
    for item in raw_outcomes:
        try:
            if isinstance(item, FieldOutcome):
                outcomes.append(item)
            elif isinstance(item, dict):
                outcomes.append(FieldOutcome.model_validate(item))
        except Exception:
            continue
    return outcomes


def _coerce_detection_result(raw_detection: Any, run_id: str, apply_url: str, platform_id: str) -> DetectionResult:
    detection_payload = dict(raw_detection) if isinstance(raw_detection, dict) else {}
    if not detection_payload:
        detection_payload = {
            "run_id": run_id,
            "platform_id": platform_id,
            "apply_url": apply_url,
            "used_iframe": False,
            "detection_method": "fallback",
        }
    return DetectionResult.model_validate(detection_payload)


def _started_at_iso(payload: dict[str, Any]) -> str:
    for key in ("started_at", "created_at"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return _utcnow_iso()


def _platform_id(form_session: dict[str, Any], listing: dict[str, Any]) -> str:
    detection = form_session.get("detection") if isinstance(form_session.get("detection"), dict) else {}
    return str(detection.get("platform_id") or listing.get("platform") or "unknown")


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
    await asyncio.sleep(0)


def _is_target_closed_error(exc: Exception) -> bool:
    return "Target page, context or browser has been closed" in str(exc)


async def _ensure_submission_page_ready(
    *,
    page: Any,
    adapter: Any,
    apply_url: str,
    detection: dict[str, Any],
) -> dict[str, Any]:
    try:
        if await _needs_navigation_rehydrate(page):
            return {
                "success": False,
                "error": "Shared browser page is empty or unavailable for form submission",
                "error_type": "PreconditionError",
            }
        return {"success": True}
    except Exception as exc:
        return {
            "success": False,
            "error": f"Failed to prepare submission page at {apply_url}: {exc}",
            "error_type": exc.__class__.__name__,
        }


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
