from __future__ import annotations

from autorole.integrations.form_controls.adapters.base import ATSAdapter, AdapterConfig, PageSection


class WorkdayAdapter(ATSAdapter):
	platform_id = "workday"
	config = AdapterConfig(
		apply_button_selector='a[data-automation-id="applyNowButton"]',
		next_button_selector='button[data-automation-id="bottom-navigation-next-button"]',
		submit_button_selector='button[data-automation-id="bottom-navigation-submit-button"]',
	)

	async def setup(self, page: object, frame: object | None) -> None:
		_ = frame
		if hasattr(page, "wait_for_selector"):
			await page.wait_for_selector('[data-automation-id="formContainer"]', timeout=15_000)

	async def get_current_page_section(self, page: object) -> PageSection:
		step_title = "Workday step"
		if hasattr(page, "locator"):
			try:
				text = await page.locator('[data-automation-id="currentStepTitle"]').first.text_content(timeout=3_000)
				if text:
					step_title = text.strip() or step_title
			except Exception:
				pass
		return PageSection(label=step_title, root='[data-automation-id="formContainer"]')

	async def advance(self, page: object) -> str:
		if not hasattr(page, "locator"):
			return "submit"
		submit_visible = await page.locator(self.config.submit_button_selector).first.is_visible()
		if submit_visible:
			await page.click(self.config.submit_button_selector)
			return "submit"
		await page.click(self.config.next_button_selector)
		if hasattr(page, "wait_for_load_state"):
			await page.wait_for_load_state("networkidle")
		return "next_page"

	async def get_file_input(self, page: object) -> object | None:
		if not hasattr(page, "locator"):
			return None
		loc = page.locator('[data-automation-id="file-upload-input"]').first
		if await loc.count() > 0:
			return loc
		fallback = page.locator('input[type="file"]').first
		return fallback if await fallback.count() > 0 else None

	async def confirm_success(self, page: object) -> bool:
		if hasattr(page, "wait_for_selector"):
			try:
				await page.wait_for_selector('[data-automation-id="thankYouSection"]', timeout=10_000)
				return True
			except Exception:
				pass
		content = (await page.content()).lower() if hasattr(page, "content") else ""
		return "thank you" in content or "application submitted" in content
