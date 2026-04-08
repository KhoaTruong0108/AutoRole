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


def test_launch_shared_browser_retries_with_new_port_on_cdp_refused(monkeypatch) -> None:
    waits = {"count": 0}
    launched_ports: list[int] = []
    terminated_pids: list[int] = []

    class _Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    async def _fake_wait_for_cdp_ready(endpoint: str, timeout_seconds: float = 15.0) -> None:
        waits["count"] += 1
        if waits["count"] == 1:
            raise RuntimeError(
                "Timed out waiting for shared browser CDP endpoint "
                f"{endpoint.rstrip('/')}/json/version: <urlopen error [Errno 61] Connection refused>"
            )

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

    def _fake_launch_chrome(**kwargs):
        launched_ports.append(int(kwargs["port"]))
        return _Process(pid=900 + len(launched_ports))

    monkeypatch.setattr(shared_browser, "_prepare_shared_browser_metadata", lambda config, correlation_id, metadata: (dict(metadata), None))
    monkeypatch.setattr(shared_browser, "_resolve_port", lambda correlation_id, metadata: 2827)
    monkeypatch.setattr(shared_browser, "_find_open_port", lambda: 3827)
    monkeypatch.setattr(shared_browser, "_launch_chrome", _fake_launch_chrome)
    monkeypatch.setattr(shared_browser, "_terminate_process_tree", lambda pid: terminated_pids.append(pid))
    monkeypatch.setattr(shared_browser, "_wait_for_cdp_ready", _fake_wait_for_cdp_ready)
    monkeypatch.setattr(shared_browser, "connect_shared_browser_page", _fake_connect_shared_browser_page)
    monkeypatch.setattr(shared_browser, "close_managed_browser_page", _fake_close_managed_browser_page)

    descriptor = asyncio.run(
        shared_browser.launch_shared_browser(
            correlation_id="corr-cdp-refused-1",
            metadata={"shared_browser_cdp_port": 2827},
            listing={
                "platform": "greenhouse",
                "apply_url": "https://job-boards.greenhouse.io/company/jobs/1",
            },
            authenticated=False,
        )
    )

    assert descriptor["status"] == "ready"
    assert descriptor["port"] == 3827
    assert descriptor.get("launch_retry_attempt") == 2
    assert launched_ports == [2827, 3827]
    assert terminated_pids == [901]