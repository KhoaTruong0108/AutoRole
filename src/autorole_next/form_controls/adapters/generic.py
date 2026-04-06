from __future__ import annotations

from autorole_next.form_controls.adapters.base import ATSAdapter, AdapterConfig, PageSection


class GenericAdapter(ATSAdapter):
	platform_id = "generic"
	config = AdapterConfig(
		apply_button_selector=None,
		next_button_selector='button[type="submit"], input[type="submit"]',
		submit_button_selector='button[type="submit"], input[type="submit"]',
	)

	async def setup(self, page: object, frame: object | None) -> None:
		_ = (page, frame)

	async def get_current_page_section(self, page: object) -> PageSection:
		_ = page
		return PageSection(label="Full page (generic)", root="body")

	async def advance(self, page: object) -> str:
		if hasattr(page, "click"):
			await page.click(self.config.submit_button_selector)
		return "submit"

	async def get_file_input(self, page: object) -> object | None:
		if not hasattr(page, "locator"):
			return None
		loc = page.locator('input[type="file"]').first
		return loc if await loc.count() > 0 else None

	async def confirm_success(self, page: object) -> bool:
		if hasattr(page, "wait_for_timeout"):
			await page.wait_for_timeout(3_000)
		content = (await page.content()).lower() if hasattr(page, "content") else ""
		return "thank you" in content or "application received" in content or "application submitted" in content
