from __future__ import annotations

import asyncio
from pathlib import Path

from autorole_next.integrations import shared_browser


class _FakeProcess:
    def __init__(self, pid: int = 999) -> None:
        self.pid = pid


class _FakePage:
    def __init__(self) -> None:
        self.url = ""

    async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        assert wait_until == "domcontentloaded"
        assert timeout == 60_000
        self.url = url


def test_launch_shared_browser_retries_transient_connect_reset(monkeypatch) -> None:
    async def _fake_wait_for_cdp_ready(endpoint: str, timeout_seconds: float = 15.0) -> None:
        assert endpoint.startswith("http://127.0.0.1:")

    async def _fake_sleep(_: float) -> None:
        return None

    async def _fake_close_managed_browser_page(managed: dict[str, object] | None, *, close_remote: bool = False) -> None:
        return None

    attempts = {"count": 0}
    page = _FakePage()

    async def _fake_connect_shared_browser_page(shared_browser_descriptor: dict[str, object]) -> dict[str, object]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return {
                "success": False,
                "error": "[Errno 54] Connection reset by peer",
                "error_type": "ConnectionResetError",
            }
        return {
            "success": True,
            "kind": "shared_browser",
            "playwright": object(),
            "browser": object(),
            "context": object(),
            "page": page,
        }

    monkeypatch.setattr(shared_browser, "_prepare_shared_browser_metadata", lambda config, correlation_id, metadata: (dict(metadata), None))
    monkeypatch.setattr(shared_browser, "_launch_chrome", lambda **kwargs: _FakeProcess())
    monkeypatch.setattr(shared_browser, "_wait_for_cdp_ready", _fake_wait_for_cdp_ready)
    monkeypatch.setattr(shared_browser, "connect_shared_browser_page", _fake_connect_shared_browser_page)
    monkeypatch.setattr(shared_browser, "close_managed_browser_page", _fake_close_managed_browser_page)
    monkeypatch.setattr(shared_browser.asyncio, "sleep", _fake_sleep)

    descriptor = asyncio.run(
        shared_browser.launch_shared_browser(
            correlation_id="corr-retry-1",
            metadata={},
            listing={
                "platform": "workday",
                "apply_url": "https://company.workday.com/jobs/1/apply",
            },
            authenticated=False,
        )
    )

    assert attempts["count"] == 2
    assert descriptor["status"] == "ready"
    assert descriptor["current_url"] == "https://company.workday.com/jobs/1/apply"


def test_launch_shared_browser_passes_path_profile_dir(monkeypatch) -> None:
    async def _fake_wait_for_cdp_ready(endpoint: str, timeout_seconds: float = 15.0) -> None:
        assert endpoint.startswith("http://127.0.0.1:")

    async def _fake_close_managed_browser_page(managed: dict[str, object] | None, *, close_remote: bool = False) -> None:
        return None

    async def _fake_connect_shared_browser_page(shared_browser_descriptor: dict[str, object]) -> dict[str, object]:
        return {
            "success": True,
            "kind": "shared_browser",
            "playwright": object(),
            "browser": object(),
            "context": object(),
            "page": _FakePage(),
        }

    seen: dict[str, object] = {}

    def _fake_launch_chrome(**kwargs):
        seen.update(kwargs)
        return _FakeProcess()

    monkeypatch.setattr(shared_browser, "_prepare_shared_browser_metadata", lambda config, correlation_id, metadata: (dict(metadata), None))
    monkeypatch.setattr(shared_browser, "_launch_chrome", _fake_launch_chrome)
    monkeypatch.setattr(shared_browser, "_wait_for_cdp_ready", _fake_wait_for_cdp_ready)
    monkeypatch.setattr(shared_browser, "connect_shared_browser_page", _fake_connect_shared_browser_page)
    monkeypatch.setattr(shared_browser, "close_managed_browser_page", _fake_close_managed_browser_page)

    descriptor = asyncio.run(
        shared_browser.launch_shared_browser(
            correlation_id="corr-path-1",
            metadata={},
            listing={
                "platform": "greenhouse",
                "apply_url": "https://company.greenhouse.io/jobs/1",
            },
            authenticated=False,
        )
    )

    assert descriptor["status"] == "ready"
    assert Path(str(seen["profile_dir"])).name