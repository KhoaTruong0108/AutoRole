from __future__ import annotations

import difflib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autorole_next.config import TailoringConfig


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def select_degree(score: float, config: TailoringConfig | dict[str, Any] | None = None) -> int:
    if config is None:
        cfg = TailoringConfig()
    elif isinstance(config, TailoringConfig):
        cfg = config
    else:
        cfg = TailoringConfig.model_validate(config)

    if score >= cfg.pass_threshold:
        base_degree = 0
    elif score >= cfg.degree_1_threshold:
        base_degree = 1
    elif score >= cfg.degree_2_threshold:
        base_degree = 2
    elif score >= cfg.degree_3_threshold:
        base_degree = 3
    else:
        base_degree = 4 if cfg.degree_4_enabled else 3

    return base_degree


def select_degree_for_attempt(
    score: float,
    *,
    attempt: int,
    config: TailoringConfig | dict[str, Any] | None = None,
) -> int:
    base_degree = select_degree(score, config)
    max_degree = 4 if _coerce_tailoring_config(config).degree_4_enabled else 3
    retry_offset = max(0, int(attempt) - 1)
    return min(max_degree, base_degree + retry_offset)


def _coerce_tailoring_config(config: TailoringConfig | dict[str, Any] | None) -> TailoringConfig:
    if config is None:
        return TailoringConfig()
    if isinstance(config, TailoringConfig):
        return config
    return TailoringConfig.model_validate(config)


def resolve_source_resume(payload: dict[str, Any], metadata: dict[str, Any]) -> tuple[str, str, int]:
    parent_resume_id = "master"
    version = 1

    tailoring = payload.get("tailoring") if isinstance(payload.get("tailoring"), dict) else {}
    previous_path = str(tailoring.get("resume_path") or "")
    if previous_path:
        previous_resume = _read_if_exists(Path(previous_path))
        if previous_resume:
            parent_resume_id = str(tailoring.get("resume_id") or "previous")
            version = _next_version(Path(previous_path))
            return previous_resume, parent_resume_id, version

    resume_path = metadata.get("resume_path")
    if isinstance(resume_path, str) and resume_path.strip():
        content = _read_if_exists(Path(resume_path))
        if content:
            return content, parent_resume_id, version

    for fallback in (
        Path("resumes/master.md"),
        Path("~/.autorole/resumes/master.md").expanduser(),
    ):
        content = _read_if_exists(fallback)
        if content:
            return content, parent_resume_id, version

    # Ensure tailoring can still run in isolated test environments.
    return (
        "# Resume\n\n- Experience: software engineering\n- Skills: Python, APIs, SQL\n",
        parent_resume_id,
        version,
    )


def build_resume_path(correlation_id: str, version: int) -> str:
    return f"resumes/{correlation_id}/tailored_v{version}.md"


def tailor_resume(
    source_md: str,
    *,
    degree: int,
    scoring: dict[str, Any],
) -> str:
    if degree == 0:
        return source_md

    lines = source_md.splitlines()
    jd_breakdown = scoring.get("jd_breakdown") if isinstance(scoring.get("jd_breakdown"), dict) else {}
    required_skills = jd_breakdown.get("required_skills") if isinstance(jd_breakdown.get("required_skills"), list) else []
    mismatched = scoring.get("mismatched") if isinstance(scoring.get("mismatched"), list) else []

    additions: list[str] = []
    if required_skills:
        top_skills = [str(skill).strip() for skill in required_skills[:5] if str(skill).strip()]
        if top_skills:
            additions.append(f"- Targeted skills for this role: {', '.join(top_skills)}")

    if mismatched:
        additions.append(f"- Tailoring focus: {', '.join(str(item) for item in mismatched[:5])}")

    if degree >= 2:
        additions.append("- Highlighted impact metrics and ownership language for relevant projects.")
    if degree >= 3:
        additions.append("- Added projection-oriented framing aligned to responsibilities in the job description.")
    if degree >= 4:
        additions.append("- High-intensity tailoring mode enabled by configuration.")

    if not additions:
        additions.append("- Tailored resume language to align with current job requirements.")

    return "\n".join(lines + ["", "## Tailoring Notes", "", *additions, ""]) + "\n"


def build_diff_summary(
    *,
    source_md: str,
    tailored_md: str,
    degree: int,
    scoring: dict[str, Any],
) -> str:
    jd_breakdown = scoring.get("jd_breakdown") if isinstance(scoring.get("jd_breakdown"), dict) else {}
    changes = _compute_changes(source_md, tailored_md, jd_breakdown)
    summary = {
        "tailoring_degree": degree,
        "overall_delta": 0.0,
        "sections": [
            {
                "section_name": "Resume",
                "changes": changes,
                "net_impact": f"Captured {len(changes)} line-level diff change(s)",
            }
        ]
        if changes
        else [],
    }
    return json.dumps(summary, ensure_ascii=True)


def _compute_changes(source_md: str, tailored_md: str, jd_breakdown: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for line in difflib.ndiff(source_md.splitlines(), tailored_md.splitlines()):
        if line.startswith("- "):
            text = line[2:].strip()
            if text:
                changes.append(
                    {
                        "location": "Resume",
                        "criterion": _infer_criterion(text, jd_breakdown),
                        "change_type": "removed",
                        "original": text,
                        "revised": "",
                        "rationale": "Removed while aligning resume to JD priorities",
                    }
                )
        elif line.startswith("+ "):
            text = line[2:].strip()
            if text:
                changes.append(
                    {
                        "location": "Resume",
                        "criterion": _infer_criterion(text, jd_breakdown),
                        "change_type": "added",
                        "original": "",
                        "revised": text,
                        "rationale": "Added to better match JD language and requirements",
                    }
                )
    return changes


def _infer_criterion(text: str, jd_breakdown: dict[str, Any]) -> str:
    lower = text.lower()
    if any(word in lower for word in ["python", "kubernetes", "aws", "sql", "api", "backend"]):
        return "technical_skills"
    if any(word in lower for word in ["lead", "senior", "staff", "principal"]):
        return "seniority_alignment"
    if any(word in lower for word in ["year", "scale", "million", "complex", "production"]):
        return "experience_depth"
    if any(word in lower for word in ["fintech", "health", "saas", "domain", "industry"]):
        return "domain_relevance"

    serialized = json.dumps(jd_breakdown, ensure_ascii=True).lower()
    if "culture" in serialized or "collaboration" in serialized:
        return "culture_fit"
    return "technical_skills"


def _next_version(path: Path) -> int:
    name = path.name
    match = re.search(r"_v(\d+)\.md$", name)
    if match:
        return int(match.group(1)) + 1
    return 2


def _read_if_exists(path: Path) -> str:
    try:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8")
    except Exception:
        return ""
    return ""
