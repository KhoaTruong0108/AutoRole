from __future__ import annotations

from pathlib import Path
from typing import Any


class ExternalPackageFormExtractor:
    """Bridge to external sync scraper utility added under autorole.form_controls."""

    async def extract(self, page: Any) -> dict[str, Any]:
        _ = page
        raise NotImplementedError(
            "ExternalPackageFormExtractor requires URL-driven invocation and is not page-bound. "
            "Use scrape_url(...) instead."
        )

    async def scrape_url(self, url: str, *, headless: bool = True, timeout_ms: int = 60000) -> dict[str, Any]:
        from autorole.form_controls.web_form_scraper import ScrapeOptions, scrape_webpage_form

        options = ScrapeOptions(headless=headless, timeout_ms=timeout_ms)
        return scrape_webpage_form(url=url, options=options)


class ExternalPackageFormApplier:
    """Bridge to external sync applier utility added under autorole.form_controls."""

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
        from autorole.form_controls.web_form_applier import apply_blueprint_on_url

        result = apply_blueprint_on_url(
            url,
            blueprint,
            headless=headless,
            slow_mo=0,
            timeout_ms=timeout_ms,
        )
        if output_path:
            Path(output_path).write_text(str(result), encoding="utf-8")
        return result
