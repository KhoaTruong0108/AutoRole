from __future__ import annotations

from typing import Any


class AsyncDOMFormExtractor:
    """Default async Playwright form extractor used by form_intelligence stage."""

    async def extract(self, page: Any) -> dict[str, Any]:
        elements = await page.query_selector_all("input, select, textarea")
        fields: list[dict[str, Any]] = []
        for element in elements:
            name = await element.get_attribute("name") or await element.get_attribute("id")
            if not name:
                continue
            tag = await element.evaluate("el => el.tagName.toLowerCase()")
            type_attr = await element.get_attribute("type")
            label = await element.get_attribute("aria-label") or name
            required = (await element.get_attribute("required")) is not None

            field_type = "text"
            options: list[str] = []
            if tag == "select":
                field_type = "single_choice"
                options = await element.evaluate(
                    "el => Array.from(el.options).map(o => o.textContent?.trim() ?? '')"
                )
            elif type_attr in {"checkbox"}:
                field_type = "multiple_choice"
            elif type_attr in {"radio"}:
                field_type = "single_choice"
            elif type_attr == "file":
                field_type = "file_upload"

            fields.append(
                {
                    "id": name,
                    "label": label,
                    "type": field_type,
                    "required": required,
                    "options": options,
                    "value": "" if field_type != "multiple_choice" else [],
                }
            )

        return {"fields": fields}
