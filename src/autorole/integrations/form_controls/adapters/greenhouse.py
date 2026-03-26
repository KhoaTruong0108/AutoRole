from __future__ import annotations

from autorole.integrations.form_controls.adapters.base import ATSAdapter, AdapterConfig, PageSection


class GreenhouseAdapter(ATSAdapter):
	platform_id = "greenhouse"
	config = AdapterConfig(
		apply_button_selector='a[href*="/apply"]',
		next_button_selector="",
		submit_button_selector="#submit_app",
	)

	async def setup(self, page: object, frame: object | None) -> None:
		_ = frame
		if hasattr(page, "locator"):
			cookie_btn = page.locator('[id*="cookie"] button').first
			try:
				if await cookie_btn.is_visible(timeout=2_000):
					await cookie_btn.click()
			except Exception:
				return

	async def get_current_page_section(self, page: object) -> PageSection:
		_ = page
		return PageSection(label="Application form", root="form#application_form")

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
			await page.wait_for_timeout(2_000)
		content = (await page.content()).lower() if hasattr(page, "content") else ""
		return "application submitted" in content or "thank you" in content
