from __future__ import annotations

import pytest

from autorole_next.form_controls.adapters.greenhouse import GreenhouseAdapter


class _FakePage:
    def __init__(self) -> None:
        self.wait_calls: list[tuple[str, str, int]] = []
        self.click_calls: list[str] = []

    async def wait_for_selector(self, selector: str, *, state: str, timeout: int) -> None:
        self.wait_calls.append((selector, state, timeout))

    async def click(self, selector: str) -> None:
        self.click_calls.append(selector)


@pytest.mark.asyncio
async def test_greenhouse_advance_waits_for_submit_button_and_clicks_submit() -> None:
    page = _FakePage()
    adapter = GreenhouseAdapter()

    action = await adapter.advance(page)

    assert action == "submit"
    assert page.wait_calls == [('.application--submit button[type="submit"]', "visible", 10_000)]
    assert page.click_calls == ['.application--submit button[type="submit"]']