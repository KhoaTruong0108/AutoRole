from __future__ import annotations

import asyncio
import os
import socket
import shutil
import tempfile
import urllib.error
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from autorole_next.config import AppConfig

from .llm_apply import _launch_chrome, _terminate_process_tree


SESSION_BROWSER_BASE_CDP_PORT = 2242
SESSION_BROWSER_CONNECT_TIMEOUT_SECONDS = 15.0
SESSION_BROWSER_CONNECT_RETRY_ATTEMPTS = 4
SESSION_BROWSER_CONNECT_RETRY_DELAY_SECONDS = 1
SESSION_BROWSER_LAUNCH_RETRY_ATTEMPTS = 3
_VOLATILE_CHROME_ENTRIES = {
    "ShaderCache",
    "GrShaderCache",
    "Service Worker",
    "Cache",
    "Code Cache",
    "GPUCache",
    "CacheStorage",
    "Crashpad",
    "BrowserMetrics",
    "SafeBrowsing",
    "SingletonLock",
    "SingletonSocket",
    "SingletonCookie",
    "RunningChromeVersion",
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in value.strip().lower())
    collapsed = "-".join(part for part in cleaned.split("-") if part)
    return collapsed or "job"


def _resolve_port(correlation_id: str, metadata: dict[str, Any]) -> int:
    raw_port = metadata.get("shared_browser_cdp_port")
    if raw_port is not None:
        try:
            return int(raw_port)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid shared_browser_cdp_port: {raw_port}") from exc
    return SESSION_BROWSER_BASE_CDP_PORT + (zlib.crc32(correlation_id.encode("utf-8")) % 1000)


def _profile_dir(config: AppConfig, correlation_id: str) -> Path:
    runtime_root = Path(config.base_dir).expanduser() / "shared_browser"
    profile_dir = runtime_root / "profiles" / _slug(correlation_id)
    profile_dir.parent.mkdir(parents=True, exist_ok=True)
    return profile_dir


def _profile_dir_for_attempt(config: AppConfig, correlation_id: str, attempt: int) -> Path:
    base = _profile_dir(config, correlation_id)
    if attempt <= 1:
        return base
    retried = base.parent / f"{base.name}-retry-{attempt}"
    retried.parent.mkdir(parents=True, exist_ok=True)
    return retried


def _find_open_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _seed_dir(config: AppConfig, correlation_id: str) -> Path:
    runtime_root = Path(config.base_dir).expanduser() / "shared_browser"
    seed_dir = runtime_root / "seed_profiles" / _slug(correlation_id)
    seed_dir.parent.mkdir(parents=True, exist_ok=True)
    return seed_dir


def _temporary_seed_dir(config: AppConfig, correlation_id: str) -> Path:
    runtime_root = Path(config.base_dir).expanduser() / "shared_browser"
    temp_root = runtime_root / "seed_profiles"
    temp_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f"{_slug(correlation_id)}-", dir=temp_root))


def _log_path(correlation_id: str) -> Path:
    path = Path("logs") / "session" / correlation_id / "chrome-output.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _resolve_chrome_user_data_source(metadata: dict[str, Any]) -> Path | None:
    configured = str(metadata.get("chrome_user_data") or os.environ.get("AR_CHROME_USER_DATA") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return None


def _copy_chrome_user_data(source: Path, destination: Path) -> None:
    nested_ignore = shutil.ignore_patterns(
        "Cache",
        "Code Cache",
        "GPUCache",
        "Service Worker",
        "RunningChromeVersion",
    )

    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name in _VOLATILE_CHROME_ENTRIES:
            continue
        target = destination / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True, ignore=nested_ignore)
            else:
                shutil.copy2(item, target)
        except (FileNotFoundError, PermissionError, OSError):
            continue


def _prepare_shared_browser_metadata(
    config: AppConfig,
    correlation_id: str,
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], Path | None]:
    launch_metadata = dict(metadata)
    source = _resolve_chrome_user_data_source(metadata)
    if source is None or not source.exists():
        seed_dir = _temporary_seed_dir(config, correlation_id)
        launch_metadata["chrome_user_data"] = str(seed_dir)
        return launch_metadata, seed_dir

    seed_dir = _seed_dir(config, correlation_id)
    shutil.rmtree(seed_dir, ignore_errors=True)
    _copy_chrome_user_data(source, seed_dir)
    launch_metadata["chrome_user_data"] = str(seed_dir)
    return launch_metadata, seed_dir


def resolve_shared_browser(payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
    session_payload = payload.get("session") if isinstance(payload.get("session"), dict) else {}
    candidates = [
        payload.get("shared_browser"),
        session_payload.get("shared_browser"),
    ]
    if isinstance(metadata, dict):
        candidates.append(metadata.get("shared_browser"))
    for candidate in candidates:
        if isinstance(candidate, dict):
            return dict(candidate)
    return None


def shared_browser_requested(metadata: dict[str, Any], listing: dict[str, Any]) -> bool:
    raw_enabled = metadata.get("shared_browser_enabled")
    if raw_enabled is not None and not bool(raw_enabled):
        return False

    apply_url = str(listing.get("apply_url") or listing.get("job_url") or "").strip()
    hostname = urlsplit(apply_url).hostname or ""
    synthetic_hosts = {"", "example.com", "example.org", "example.net", "localhost", "127.0.0.1"}
    return hostname.lower() not in synthetic_hosts


def shared_browser_ready(shared_browser: dict[str, Any] | None) -> bool:
    if not isinstance(shared_browser, dict):
        return False
    if str(shared_browser.get("status") or "") != "ready":
        return False
    endpoint = str(shared_browser.get("endpoint") or "").strip()
    return bool(endpoint)


def _is_transient_shared_browser_error(error: BaseException | str) -> bool:
    message = str(error).lower()
    transient_markers = (
        "connection reset by peer",
        "econnreset",
        "target page, context or browser has been closed",
        "browser has been closed",
        "websocket closed",
        "ws closed",
    )
    return any(marker in message for marker in transient_markers)


def _is_launch_conflict_error(error: BaseException | str) -> bool:
    message = str(error).lower()
    markers = (
        "timed out waiting for shared browser cdp endpoint",
        "connection refused",
        "address already in use",
        "eaddrinuse",
        "singletonlock",
        "singletonsocket",
        "singletoncookie",
        "chrome failed to start",
    )
    return any(marker in message for marker in markers)


async def _connect_shared_browser_page_with_retry(
    descriptor: dict[str, Any],
    apply_url: str,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, SESSION_BROWSER_CONNECT_RETRY_ATTEMPTS + 1):
        managed = await connect_shared_browser_page(descriptor)
        if not managed.get("success", False):
            error = RuntimeError(str(managed.get("error") or "Failed to connect to shared browser"))
            last_error = error
            if attempt < SESSION_BROWSER_CONNECT_RETRY_ATTEMPTS and _is_transient_shared_browser_error(error):
                await asyncio.sleep(SESSION_BROWSER_CONNECT_RETRY_DELAY_SECONDS * attempt)
                continue
            raise error

        page = managed.get("page")
        try:
            if page is not None and apply_url:
                await page.goto(apply_url, wait_until="domcontentloaded", timeout=60_000)
                descriptor["current_url"] = str(getattr(page, "url", "") or apply_url)
            else:
                descriptor["current_url"] = apply_url
            return managed
        except Exception as exc:
            last_error = exc
            await close_managed_browser_page(managed)
            if attempt < SESSION_BROWSER_CONNECT_RETRY_ATTEMPTS and _is_transient_shared_browser_error(exc):
                await asyncio.sleep(SESSION_BROWSER_CONNECT_RETRY_DELAY_SECONDS * attempt)
                continue
            raise

    raise last_error or RuntimeError("Failed to connect to shared browser")


async def launch_shared_browser(
    *,
    correlation_id: str,
    metadata: dict[str, Any],
    listing: dict[str, Any],
    authenticated: bool,
) -> dict[str, Any]:
    config = AppConfig()
    launch_metadata, seeded_user_data_dir = _prepare_shared_browser_metadata(config, correlation_id, metadata)
    chrome_log_path = _log_path(correlation_id)
    apply_url = str(listing.get("apply_url") or listing.get("job_url") or "")
    platform = str(listing.get("platform") or "unknown")
    preferred_port = _resolve_port(correlation_id, metadata)

    last_error: Exception | None = None
    for attempt in range(1, SESSION_BROWSER_LAUNCH_RETRY_ATTEMPTS + 1):
        port = preferred_port if attempt == 1 else _find_open_port()
        endpoint = f"http://127.0.0.1:{port}"
        profile_dir = _profile_dir_for_attempt(config, correlation_id, attempt)

        with chrome_log_path.open("ab") as log_stream:
            process = _launch_chrome(
                profile_dir=profile_dir,
                port=port,
                metadata=launch_metadata,
                log_stream=log_stream,
            )

        descriptor = {
            "kind": "shared_browser",
            "status": "starting",
            "endpoint": endpoint,
            "port": port,
            "pid": int(process.pid),
            "profile_dir": str(profile_dir),
            "chrome_log_path": str(chrome_log_path),
            "platform": platform,
            "apply_url": apply_url,
            "authenticated": bool(authenticated),
            "launched_at": _utcnow_iso(),
        }
        if seeded_user_data_dir is not None:
            descriptor["seeded_user_data_dir"] = str(seeded_user_data_dir)
        if attempt > 1:
            descriptor["launch_retry_attempt"] = attempt

        try:
            await _wait_for_cdp_ready(endpoint)
            managed = await _connect_shared_browser_page_with_retry(descriptor, apply_url)
            await close_managed_browser_page(managed)
            descriptor["status"] = "ready"
            return descriptor
        except Exception as exc:
            last_error = exc
            _terminate_process_tree(int(process.pid))
            if attempt < SESSION_BROWSER_LAUNCH_RETRY_ATTEMPTS and _is_launch_conflict_error(exc):
                continue
            raise

    raise last_error or RuntimeError("Failed to launch shared browser")


async def connect_shared_browser_page(shared_browser: dict[str, Any]) -> dict[str, Any]:
    endpoint = str(shared_browser.get("endpoint") or "").strip()
    if not endpoint:
        return {
            "success": False,
            "error": "shared browser endpoint is missing",
            "error_type": "PreconditionError",
        }

    try:
        from playwright.async_api import async_playwright
    except Exception as exc:
        return {
            "success": False,
            "error": "Playwright is required for shared browser CDP connections",
            "error_type": exc.__class__.__name__,
        }

    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.connect_over_cdp(endpoint)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        pages = list(getattr(context, "pages", []))
        page = pages[0] if pages else await context.new_page()
        return {
            "success": True,
            "kind": "shared_browser",
            "playwright": playwright,
            "browser": browser,
            "context": context,
            "page": page,
        }
    except Exception as exc:
        return {
            "success": False,
            "error": f"Failed to connect to shared browser at {endpoint}: {exc}",
            "error_type": exc.__class__.__name__,
        }


async def close_managed_browser_page(managed: dict[str, Any] | None, *, close_remote: bool = False) -> None:
    if not managed or not managed.get("success"):
        return
    kind = str(managed.get("kind") or "")
    playwright = managed.get("playwright")
    browser = managed.get("browser")
    context = managed.get("context")
    page = managed.get("page")

    if kind != "shared_browser":
        try:
            if page is not None and hasattr(page, "close"):
                await page.close()
        except Exception:
            pass
        try:
            if context is not None:
                await context.close()
        except Exception:
            pass
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
    elif close_remote:
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass

    try:
        if playwright is not None:
            await playwright.stop()
    except Exception:
        pass


async def shutdown_shared_browser(shared_browser: dict[str, Any]) -> dict[str, Any]:
    result = dict(shared_browser)
    result["closed_at"] = _utcnow_iso()
    result["closed_remote"] = False

    managed = await connect_shared_browser_page(shared_browser)
    if managed.get("success", False):
        try:
            await close_managed_browser_page(managed, close_remote=True)
            result["closed_remote"] = True
        except Exception as exc:
            result["status"] = "close_error"
            result["error"] = str(exc)
            result["error_type"] = exc.__class__.__name__
    else:
        result["error"] = str(managed.get("error") or "")
        result["error_type"] = str(managed.get("error_type") or "")

    pid = int(result.get("pid") or 0)
    if pid > 0 and _process_alive(pid):
        _terminate_process_tree(pid)

    result["status"] = "closed"
    return result


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


async def _wait_for_cdp_ready(endpoint: str, timeout_seconds: float = SESSION_BROWSER_CONNECT_TIMEOUT_SECONDS) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    probe = endpoint.rstrip("/") + "/json/version"
    last_error = ""
    while asyncio.get_running_loop().time() < deadline:
        try:
            with urllib.request.urlopen(probe, timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = str(exc)
        await asyncio.sleep(0.25)
    raise RuntimeError(f"Timed out waiting for shared browser CDP endpoint {probe}: {last_error}")