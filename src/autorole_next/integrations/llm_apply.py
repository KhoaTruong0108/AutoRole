from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import signal
import subprocess
import zlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

from autorole_next.config import AppConfig
from autorole_next.form_controls.profile import UserProfile, load_profile

BASE_CDP_PORT = 9222
DEFAULT_LLM_APPLY_TIMEOUT_SECONDS = 900.0
CLAUDE_DISALLOWED_TOOLS = (
    "mcp__gmail__draft_email,mcp__gmail__modify_email,"
    "mcp__gmail__delete_email,mcp__gmail__download_attachment,"
    "mcp__gmail__batch_modify_emails,mcp__gmail__batch_delete_emails,"
    "mcp__gmail__create_label,mcp__gmail__update_label,"
    "mcp__gmail__delete_label,mcp__gmail__get_or_create_label,"
    "mcp__gmail__list_email_labels,mcp__gmail__create_filter,"
    "mcp__gmail__list_filters,mcp__gmail__get_filter,"
    "mcp__gmail__delete_filter"
)


class LlmApplyRuntimeError(RuntimeError):
    """Raised when the native llm-applying runtime cannot complete."""


@dataclass(slots=True)
class LlmApplyResult:
    status: str
    source_status: str
    confirmed: bool
    reason: str
    completed_at: str
    log_path: str
    raw_stream_path: str
    tool_log_path: str
    mcp_config_path: str
    prompt_path: str
    runtime_log_path: str
    raw_result_line: str
    tool_events: list[str]


@dataclass(slots=True)
class _RunArtifacts:
    worker_dir: Path
    profile_dir: Path
    log_dir: Path
    prompt_path: Path
    mcp_config_path: Path
    log_path: Path
    raw_stream_path: Path
    tool_log_path: Path
    runtime_log_path: Path
    chrome_log_path: Path


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in value.strip().lower())
    collapsed = "-".join(part for part in cleaned.split("-") if part)
    return collapsed or "job"


def _resolve_llm_apply_timeout_seconds(metadata: dict[str, Any]) -> float:
    raw_timeout = metadata.get("llm_apply_timeout_seconds")
    if raw_timeout in (None, ""):
        return DEFAULT_LLM_APPLY_TIMEOUT_SECONDS
    try:
        timeout_seconds = float(raw_timeout)
    except (TypeError, ValueError) as exc:
        raise LlmApplyRuntimeError(f"Invalid llm_apply_timeout_seconds: {raw_timeout}") from exc
    if timeout_seconds <= 0:
        raise LlmApplyRuntimeError(
            f"llm_apply_timeout_seconds must be greater than 0, got {raw_timeout}"
        )
    return timeout_seconds


def _write_output_snapshot(path: Path, output: str, *, header: str | None = None) -> None:
    content = output
    if header:
        content = header + ("\n\n" + output if output else "")
    path.write_text(content + ("\n" if content else ""), encoding="utf-8")


def _resolve_profile_path(metadata: dict[str, Any], config: AppConfig) -> Path:
    configured = metadata.get("profile_path")
    default_path = Path(config.base_dir).expanduser() / "user_profile.json"
    profile_path = Path(str(configured or default_path)).expanduser()
    if not profile_path.exists():
        raise LlmApplyRuntimeError(f"user profile not found: {profile_path}")
    return profile_path


def _prepare_run_artifacts(correlation_id: str, config: AppConfig) -> _RunArtifacts:
    runtime_root = Path(config.base_dir).expanduser() / "llm_apply"
    repo_root = Path(__file__).resolve().parents[3]
    slug = _slug(correlation_id)
    worker_dir = runtime_root / "workers" / slug
    profile_dir = runtime_root / "chrome_profiles" / slug
    log_dir = repo_root / "logs" / "llm_applying" / correlation_id
    worker_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    return _RunArtifacts(
        worker_dir=worker_dir,
        profile_dir=profile_dir,
        log_dir=log_dir,
        prompt_path=log_dir / "prompt.txt",
        mcp_config_path=log_dir / "mcp-config.json",
        log_path=log_dir / "claude-output.txt",
        raw_stream_path=log_dir / "claude-stream.jsonl",
        tool_log_path=log_dir / "tool-events.jsonl",
        runtime_log_path=log_dir / "runtime-events.jsonl",
        chrome_log_path=log_dir / "chrome-output.txt",
    )


def _resolve_port(correlation_id: str, metadata: dict[str, Any]) -> int:
    raw_port = metadata.get("llm_apply_cdp_port")
    if raw_port is None:
        return BASE_CDP_PORT + (zlib.crc32(correlation_id.encode("utf-8")) % 1000)
    try:
        return int(raw_port)
    except (TypeError, ValueError) as exc:
        raise LlmApplyRuntimeError(f"Invalid llm_apply_cdp_port: {raw_port}") from exc


def _resolve_chrome_path(metadata: dict[str, Any]) -> str:
    configured = str(metadata.get("chrome_path") or os.environ.get("AR_CHROME_PATH") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.exists():
            return str(candidate)

    system = platform.system()
    if system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
            Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    elif system == "Windows":
        local_app = Path(os.environ.get("LOCALAPPDATA", ""))
        candidates = [
            local_app / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        ]
    else:
        candidates = [
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/chromium"),
            Path("/snap/bin/chromium"),
        ]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise LlmApplyRuntimeError("Chrome executable not found; set metadata.chrome_path or AR_CHROME_PATH")


def _seed_chrome_profile(profile_dir: Path, metadata: dict[str, Any]) -> None:
    if (profile_dir / "Default").exists():
        return

    configured = str(metadata.get("chrome_user_data") or os.environ.get("AR_CHROME_USER_DATA") or "").strip()
    if configured:
        source = Path(configured).expanduser()
    elif platform.system() == "Darwin":
        source = Path.home() / "Library/Application Support/Google/Chrome"
    elif platform.system() == "Windows":
        source = Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data"
    else:
        chrome_source = Path.home() / ".config/google-chrome"
        source = chrome_source if chrome_source.exists() else Path.home() / ".config/chromium"

    if not source.exists():
        profile_dir.mkdir(parents=True, exist_ok=True)
        return

    ignore_names = shutil.ignore_patterns(
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
    )
    shutil.copytree(source, profile_dir, dirs_exist_ok=True, ignore=ignore_names)


def _terminate_process_tree(pid: int) -> None:
    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass


def _free_port(port: int) -> None:
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return

    for line in result.stdout.splitlines():
        pid = line.strip()
        if pid.isdigit():
            _terminate_process_tree(int(pid))


def _launch_chrome(
    *,
    profile_dir: Path,
    port: int,
    metadata: dict[str, Any],
    log_stream: BinaryIO,
) -> subprocess.Popen[Any]:
    profile_dir = Path(profile_dir).expanduser()
    _free_port(port)
    _seed_chrome_profile(profile_dir, metadata)
    command = [
        _resolve_chrome_path(metadata),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--disable-notifications",
        "--disable-popup-blocking",
        "--window-size=1440,960",
    ]
    if str(metadata.get("llm_apply_headless") or "").strip().lower() in {"1", "true", "yes", "on"}:
        command.append("--headless=new")

    kwargs: dict[str, Any] = {"stdout": log_stream, "stderr": log_stream}
    if platform.system() != "Windows":
        kwargs["preexec_fn"] = os.setsid
    return subprocess.Popen(command, **kwargs)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _profile_json(profile: UserProfile) -> str:
    return json.dumps(profile.model_dump(mode="json"), indent=2, ensure_ascii=True)


def _friendly_name(profile: UserProfile) -> str:
    personal = profile.personal if isinstance(profile.personal, dict) else {}
    full_name = str(personal.get("full_name") or personal.get("name") or "Candidate")
    safe = "_".join(part for part in full_name.split() if part)
    return safe or "Candidate"


def _prepare_upload_files(
    *,
    worker_dir: Path,
    profile: UserProfile,
    packaging: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[Path, Path | None, str, str]:
    uploads_dir = worker_dir / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    resume_path = Path(str(packaging.get("resume_path") or "")).expanduser()
    pdf_path = Path(str(packaging.get("pdf_path") or resume_path.with_suffix(".pdf"))).expanduser()
    if not pdf_path.exists():
        raise LlmApplyRuntimeError(f"packaged resume PDF not found: {pdf_path}")

    friendly_name = _friendly_name(profile)
    copied_resume = uploads_dir / f"{friendly_name}_Resume{pdf_path.suffix or '.pdf'}"
    shutil.copy2(str(pdf_path), str(copied_resume))

    cover_letter_payload = payload.get("cover_letter") if isinstance(payload.get("cover_letter"), dict) else {}
    cover_letter_path_raw = str(cover_letter_payload.get("pdf_path") or cover_letter_payload.get("path") or "")
    copied_cover_letter: Path | None = None
    cover_letter_text = ""
    cover_letter_pdf_path: Path | None = None
    if cover_letter_path_raw:
        cover_letter_path = Path(cover_letter_path_raw).expanduser()
        cover_letter_pdf_path = cover_letter_path if cover_letter_path.suffix.lower() == ".pdf" else cover_letter_path.with_suffix(".pdf")
        cover_letter_txt = cover_letter_path if cover_letter_path.suffix.lower() in {".txt", ".md"} else cover_letter_path.with_suffix(".txt")
        cover_letter_text = _read_text(cover_letter_txt)
        if cover_letter_pdf_path.exists():
            copied_cover_letter = uploads_dir / f"{friendly_name}_Cover_Letter{cover_letter_pdf_path.suffix}"
            shutil.copy2(str(cover_letter_pdf_path), str(copied_cover_letter))

    return copied_resume, copied_cover_letter, _read_text(resume_path), cover_letter_text


def _build_mcp_config() -> dict[str, Any]:
    return {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": ["-y", "@playwright/mcp@latest"],
            }
        }
    }


def _build_prompt(
    *,
    listing: dict[str, Any],
    profile: UserProfile,
    resume_text: str,
    cover_letter_text: str,
    resume_pdf: Path,
    cover_letter_pdf: Path | None,
    dry_run: bool,
) -> str:
    apply_url = str(listing.get("apply_url") or listing.get("job_url") or "").strip()
    title = str(listing.get("job_title") or "Unknown Role")
    company = str(listing.get("company_name") or "Unknown Company")
    platform_name = str(listing.get("platform") or "unknown")
    dry_run_instruction = (
        "Do not click the final submit/apply button; stop after validation and output RESULT:APPLIED with a short note that this was a dry run."
        if dry_run
        else "Submit the final application once every field is validated."
    )
    cover_letter_pdf_text = str(cover_letter_pdf) if cover_letter_pdf is not None else "N/A"
    cover_letter_body = cover_letter_text or (
        "No separate cover letter file is available. If the field is optional, skip it. "
        "If required, write a short truthful note using only the profile and resume."
    )
    return f"""You are an autonomous job application agent using Playwright MCP browser tools.

Goal: complete this single application accurately and safely.

JOB
- URL: {apply_url}
- Title: {title}
- Company: {company}
- Platform: {platform_name}

UPLOAD FILES
- Resume PDF: {resume_pdf}
- Cover letter PDF: {cover_letter_pdf_text}

PROFILE JSON
{_profile_json(profile)}

RESUME TEXT
{resume_text}

COVER LETTER TEXT
{cover_letter_body}

RULES
- Use only facts from the profile JSON and resume text. Never invent credentials, visas, education, or prior employers.
- If the site requires camera, microphone, payments, government ID, or unrelated marketplace onboarding, stop with RESULT:FAILED and explain briefly.
- If email verification is required, stop with RESULT:FAILED and explain briefly.
- If the role is closed or the page says applications are no longer accepted, output RESULT:EXPIRED.
- If blocked by CAPTCHA you cannot clear, output RESULT:CAPTCHA.
- If login/signup fails after a reasonable attempt, output RESULT:LOGIN_ISSUE.
- {dry_run_instruction}

PROCESS
1. Open the URL and reach the actual application form.
2. Upload the resume PDF whenever a resume upload is requested.
3. Use the cover letter PDF for file uploads, or the cover letter text for text areas.
4. Review and correct any bad ATS autofill before continuing.
5. Answer screening questions truthfully and concisely.
6. Before final submission, verify name, email, phone, location, work authorization, resume upload, and any required fields.

OUTPUT
- Emit exactly one final marker on its own line:
  RESULT:APPLIED
  RESULT:EXPIRED
  RESULT:CAPTCHA
  RESULT:LOGIN_ISSUE
  RESULT:FAILED:short_reason

Keep your reasoning short. Use the browser tools to do the work."""


def _resolve_claude_command(metadata: dict[str, Any]) -> str:
    configured = str(metadata.get("claude_path") or os.environ.get("AR_CLAUDE_PATH") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.exists():
            return str(candidate)
    discovered = shutil.which("claude")
    if discovered:
        return discovered
    raise LlmApplyRuntimeError("Claude CLI not found in PATH; install 'claude' or set metadata.claude_path")

def _hello_word_for_5_mins() -> list[str]:
    return [
        "/bin/sh",
        "-c",
        'end=$((SECONDS+120)); while [ "$SECONDS" -lt "$end" ]; do echo "Hello World"; sleep 1; done',
    ]

async def _iter_lines(stream: asyncio.StreamReader, chunk_size: int = 65536):
    """Yield lines from a stream without the 64 KB readline limit."""
    buf = b""
    while True:
        chunk = await stream.read(chunk_size)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line
    if buf:
        yield buf


async def _collect_claude_output(
    process: asyncio.subprocess.Process,
    *,
    raw_stream_path: Path,
    tool_log_path: Path,
) -> tuple[str, list[str]]:
    text_parts: list[str] = []
    tool_events: list[str] = []
    assert process.stdout is not None

    with raw_stream_path.open("w", encoding="utf-8") as raw_stream, tool_log_path.open("w", encoding="utf-8") as tool_log:
        async for line_bytes in _iter_lines(process.stdout):
            decoded_line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            if decoded_line:
                raw_stream.write(decoded_line + "\n")

            line = decoded_line.strip()
            if not line:
                continue

            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                text_parts.append(line)
                continue

            if payload.get("type") == "assistant":
                for block in payload.get("message", {}).get("content", []):
                    if block.get("type") == "text":
                        text = str(block.get("text") or "")
                        if text:
                            text_parts.append(text)
                    elif block.get("type") == "tool_use":
                        tool_name = str(block.get("name") or "tool")
                        tool_events.append(tool_name)
                        tool_log.write(
                            json.dumps(
                                {
                                    "ts": _utcnow_iso(),
                                    "seq": len(tool_events),
                                    "id": block.get("id"),
                                    "name": tool_name,
                                    "input": block.get("input"),
                                },
                                ensure_ascii=True,
                                sort_keys=True,
                            )
                            + "\n"
                        )
            elif payload.get("type") == "result":
                result_text = str(payload.get("result") or "")
                if result_text:
                    text_parts.append(result_text)

    return "\n".join(part for part in text_parts if part).strip(), tool_events


def _parse_result(output: str, *, dry_run: bool) -> tuple[str, str, bool, str]:
    for line in output.splitlines():
        clean = line.strip()
        if clean.startswith("RESULT:APPLIED"):
            if dry_run:
                return "dry_run", clean, False, "dry-run application reviewed"
            return "applied", clean, True, "application submitted"
        if clean.startswith("RESULT:EXPIRED"):
            return "expired", clean, False, "job posting is no longer accepting applications"
        if clean.startswith("RESULT:CAPTCHA"):
            return "captcha", clean, False, "captcha blocked the application flow"
        if clean.startswith("RESULT:LOGIN_ISSUE"):
            return "login_issue", clean, False, "login or signup could not be completed"
        if clean.startswith("RESULT:FAILED"):
            reason = clean.split(":", 2)[-1].strip() if clean.count(":") >= 2 else "llm apply failed"
            return "failed", clean, False, reason or "llm apply failed"
    return "failed", "", False, "Claude output did not include a RESULT marker"


async def run_llm_apply(
    *,
    correlation_id: str,
    listing: dict[str, Any],
    packaging: dict[str, Any],
    metadata: dict[str, Any],
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if shutil.which("npx") is None:
        raise LlmApplyRuntimeError("npx is required for Playwright MCP but was not found in PATH")

    config = AppConfig()
    payload = payload or {}
    profile = load_profile(_resolve_profile_path(metadata, config))
    artifacts = _prepare_run_artifacts(correlation_id, config)

    resume_pdf, cover_letter_pdf, resume_text, cover_letter_text = _prepare_upload_files(
        worker_dir=artifacts.worker_dir,
        profile=profile,
        packaging=packaging,
        payload=payload,
    )

    dry_run = True
    prompt = _build_prompt(
        listing=listing,
        profile=profile,
        resume_text=resume_text,
        cover_letter_text=cover_letter_text,
        resume_pdf=resume_pdf,
        cover_letter_pdf=cover_letter_pdf,
        dry_run=dry_run,
    )
    artifacts.prompt_path.write_text(prompt, encoding="utf-8")
    artifacts.runtime_log_path.write_text(f"started_at={_utcnow_iso()}\n", encoding="utf-8")

    chrome_log_handle = artifacts.chrome_log_path.open("ab")
    chrome_process: subprocess.Popen[Any] | None = None
    process: asyncio.subprocess.Process | None = None
    output = ""
    tool_events: list[str] = []

    try:
        port = _resolve_port(correlation_id, metadata)
        chrome_process = _launch_chrome(
            profile_dir=artifacts.profile_dir,
            port=port,
            metadata=metadata,
            log_stream=chrome_log_handle,
        )
        await asyncio.sleep(float(metadata.get("llm_apply_chrome_startup_seconds", 3.0)))
        if chrome_process.poll() is not None:
            raise LlmApplyRuntimeError(
                f"Chrome exited before MCP attach with code {chrome_process.returncode}; see {artifacts.chrome_log_path}"
            )

        artifacts.mcp_config_path.write_text(json.dumps(_build_mcp_config(), indent=2), encoding="utf-8")
        timeout_seconds = _resolve_llm_apply_timeout_seconds(metadata)
        command = [
            _resolve_claude_command(metadata),
            "--model",
            str(metadata.get("llm_apply_model") or metadata.get("claude_model") or "sonnet"),
            "-p",
            "--mcp-config",
            str(artifacts.mcp_config_path.resolve()),
            "--permission-mode",
            "bypassPermissions",
            "--no-session-persistence",
            "--disallowedTools",
            CLAUDE_DISALLOWED_TOOLS,
            "--output-format",
            "stream-json",
            "--verbose",
            "-",
        ]

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(artifacts.worker_dir),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert process.stdin is not None
        process.stdin.write(prompt.encode("utf-8"))
        await process.stdin.drain()
        process.stdin.close()

        output, tool_events = await asyncio.wait_for(
            _collect_claude_output(
                process,
                raw_stream_path=artifacts.raw_stream_path,
                tool_log_path=artifacts.tool_log_path,
            ),
            timeout=timeout_seconds,
        )
        return_code = await asyncio.wait_for(process.wait(), timeout=30)
        _write_output_snapshot(artifacts.log_path, output)

        status, raw_result_line, confirmed, reason = _parse_result(output, dry_run=dry_run)
        if return_code != 0 and status == "failed" and reason == "Claude output did not include a RESULT marker":
            reason = f"Claude exited with code {return_code}"

        completed_at = _utcnow_iso()
        artifacts.runtime_log_path.write_text(
            json.dumps(
                {
                    "completed_at": completed_at,
                    "status": status,
                    "reason": reason,
                    "tool_event_count": len(tool_events),
                },
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        return asdict(
            LlmApplyResult(
                status=status,
                source_status=status,
                confirmed=confirmed,
                reason=reason,
                completed_at=completed_at,
                log_path=str(artifacts.log_path),
                raw_stream_path=str(artifacts.raw_stream_path),
                tool_log_path=str(artifacts.tool_log_path),
                mcp_config_path=str(artifacts.mcp_config_path),
                prompt_path=str(artifacts.prompt_path),
                runtime_log_path=str(artifacts.runtime_log_path),
                raw_result_line=raw_result_line,
                tool_events=tool_events,
            )
        )
    except asyncio.TimeoutError as exc:
        timeout_seconds = _resolve_llm_apply_timeout_seconds(metadata)
        _write_output_snapshot(
            artifacts.log_path,
            output,
            header=f"llm_apply wrapper timed out after {int(timeout_seconds)} seconds",
        )
        artifacts.runtime_log_path.write_text(
            json.dumps(
                {
                    "failed_at": _utcnow_iso(),
                    "status": "timeout",
                    "timeout_seconds": timeout_seconds,
                    "tool_event_count": len(tool_events),
                },
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        raise LlmApplyRuntimeError(f"Claude apply timed out after {int(timeout_seconds)} seconds") from exc
    except Exception as exc:
        _write_output_snapshot(
            artifacts.log_path,
            output,
            header=f"llm_apply wrapper failed: {exc.__class__.__name__}: {exc}",
        )
        artifacts.runtime_log_path.write_text(
            json.dumps(
                {
                    "failed_at": _utcnow_iso(),
                    "status": "failed",
                    "error_type": exc.__class__.__name__,
                    "error": str(exc),
                    "tool_event_count": len(tool_events),
                },
                ensure_ascii=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        raise
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        if chrome_process is not None and chrome_process.poll() is None:
            _terminate_process_tree(chrome_process.pid)
        chrome_log_handle.close()
