from __future__ import annotations

import json
from typing import Any


class AsyncDOMFormApplier:
    """Default async Playwright form applier used by form_submission stage."""

    async def fill(self, page: Any, form_json_filled: dict[str, Any]) -> None:
        for field in form_json_filled.get("fields", []):
            field_id = field.get("id")
            if not field_id:
                continue

            selector = self._name_or_id_selector(field_id)
            field_type = field.get("type", "text")
            value = field.get("value")

            if field_type in {"text", "textarea", "email", "tel"}:
                if await self._is_hidden_field(page, selector):
                    continue
                await page.fill(selector, "" if value is None else str(value))
                continue

            if field_type == "single_choice":
                await self._fill_single_choice(page, field_id, "" if value is None else str(value))
                continue

            if field_type == "multiple_choice":
                values = value if isinstance(value, list) else ([] if value is None else [str(value)])
                await self._fill_multiple_choice(page, field_id, [str(v) for v in values])
                continue

            if field_type == "checkbox":
                if bool(value):
                    await page.check(selector)
                else:
                    await page.uncheck(selector)
                continue

            if field_type == "radio":
                await self._fill_radio_group(page, field_id, "" if value is None else str(value))

    async def attach_resume(self, page: Any, file_path: str) -> None:
        await page.set_input_files("input[type='file']", file_path)

    async def submit(self, page: Any) -> None:
        await page.click("button[type='submit'], input[type='submit']")
        if hasattr(page, "wait_for_load_state"):
            await page.wait_for_load_state("networkidle")

    async def confirm(self, page: Any) -> bool:
        content = (await page.content()).lower()
        return any(
            signal in content
            for signal in [
                "application submitted",
                "thank you",
                "we received",
            ]
        )

    async def _fill_single_choice(self, page: Any, field_id: str, value: str) -> None:
        selector = self._name_or_id_selector(field_id)
        first_error: Exception | None = None

        input_type = await self._get_first_input_type(page, selector)
        if input_type == "radio":
            await self._fill_radio_group(page, field_id, value)
            return
        if input_type == "checkbox":
            await self._fill_multiple_choice(page, field_id, [value] if value else [])
            return

        for option_args in ({"label": value}, {"value": value}):
            try:
                await page.select_option(selector, **option_args)
                return
            except Exception as exc:
                if first_error is None:
                    first_error = exc

        if not hasattr(page, "locator"):
            raise first_error or RuntimeError(f"single_choice field '{field_id}' could not be filled")

        field_locator = page.locator(selector)
        if await field_locator.count() > 0:
            input_like = field_locator.first
            try:
                await input_like.click()
            except Exception:
                pass
            try:
                await input_like.fill(value)
                if hasattr(input_like, "press"):
                    await input_like.press("Enter")
                return
            except Exception:
                pass

        option_locator = page.locator("[role='option']", has_text=value)
        if await option_locator.count() > 0:
            await option_locator.first.click()
            return

        exact_text = page.get_by_text(value, exact=True)
        if await exact_text.count() > 0:
            await exact_text.first.click()
            return

        raise first_error or RuntimeError(f"single_choice field '{field_id}' could not be filled")

    async def _fill_radio_group(self, page: Any, field_id: str, value: str) -> None:
        if not hasattr(page, "locator"):
            selector = (
                f"{self._name_selector(field_id)}[value={json.dumps(value)}], "
                f"{self._id_selector(field_id)}[value={json.dumps(value)}]"
            )
            await page.click(selector)
            return

        radio_group = page.locator(f"input[type='radio']{self._name_selector(field_id)}")
        if await radio_group.count() == 0:
            radio_group = page.locator(f"input[type='radio']{self._id_selector(field_id)}")
        if await radio_group.count() == 0:
            raise RuntimeError(f"radio group '{field_id}' not found")

        if value:
            candidate = page.locator(
                f"input[type='radio']{self._name_selector(field_id)}[value={json.dumps(value)}]"
            )
            if await candidate.count() > 0:
                await candidate.first.check(force=True)
                return

        for idx in range(await radio_group.count()):
            if await radio_group.nth(idx).is_checked():
                return

        await radio_group.first.check(force=True)

    async def _fill_multiple_choice(self, page: Any, field_id: str, values: list[str]) -> None:
        selector = self._name_or_id_selector(field_id)

        if not hasattr(page, "locator"):
            for option in values:
                option_selector = f"{selector}[value='{option}']"
                await page.check(option_selector)
            return

        checkbox_group = page.locator(f"input[type='checkbox']{self._name_selector(field_id)}")
        if await checkbox_group.count() == 0:
            checkbox_group = page.locator(f"input[type='checkbox']{self._id_selector(field_id)}")
        if await checkbox_group.count() == 0:
            for option in values:
                option_selector = f"{selector}[value={json.dumps(option)}]"
                await page.check(option_selector)
            return

        selected_any = False
        for option in values:
            candidate = page.locator(
                f"input[type='checkbox']{self._name_selector(field_id)}[value={json.dumps(option)}]"
            )
            if await candidate.count() > 0:
                await candidate.first.check(force=True)
                selected_any = True

        if not selected_any and await checkbox_group.count() > 0 and values:
            await checkbox_group.first.check(force=True)

    async def _get_first_input_type(self, page: Any, selector: str) -> str:
        if not hasattr(page, "locator"):
            return ""
        locator = page.locator(selector)
        if await locator.count() == 0:
            return ""
        return (await locator.first.get_attribute("type") or "").lower()

    async def _is_hidden_field(self, page: Any, selector: str) -> bool:
        if not hasattr(page, "locator"):
            return False
        locator = page.locator(selector)
        if await locator.count() == 0:
            return False
        field_type = (await locator.first.get_attribute("type") or "").lower()
        return field_type == "hidden"

    def _name_or_id_selector(self, field_id: str) -> str:
        return f"{self._name_selector(field_id)}, {self._id_selector(field_id)}"

    def _name_selector(self, field_id: str) -> str:
        return f"[name={json.dumps(str(field_id))}]"

    def _id_selector(self, field_id: str) -> str:
        return f"[id={json.dumps(str(field_id))}]"
