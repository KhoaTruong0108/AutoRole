from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field


class ScoringWeights(BaseModel):
    technical_skills: float = 0.30
    experience_depth: float = 0.25
    seniority_alignment: float = 0.20
    domain_relevance: float = 0.15
    culture_fit: float = 0.10

    def normalised(self) -> dict[str, float]:
        data = self.model_dump()
        total = sum(data.values()) or 1.0
        return {key: float(value) / total for key, value in data.items()}


class JDBreakdown(BaseModel):
    qualifications: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    culture_signals: list[str] = Field(default_factory=list)


async def fetch_jd_html(job_url: str, *, page: Any | None = None) -> str:
    if page is not None and hasattr(page, "goto") and hasattr(page, "content"):
        await page.goto(job_url, wait_until="domcontentloaded", timeout=30_000)
        return str(await page.content())

    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        response = await client.get(job_url)
        response.raise_for_status()
        return response.text


def extract_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup.find("article") or soup.find("body") or soup
    return main.get_text(separator="\n", strip=True)


def parse_jd_breakdown(jd_text: str) -> JDBreakdown:
    lines = [line.strip() for line in jd_text.splitlines() if line.strip()]
    qualifications = _select_lines(lines, r"required|qualification|must have|you have")
    responsibilities = _select_lines(lines, r"responsibil|you will|what you'll do|duties")
    preferred_skills = _select_lines(lines, r"preferred|nice to have|bonus|plus")
    culture_signals = _select_lines(lines, r"culture|value|mission|team|collaborat|inclusive")

    required_skills = _extract_skill_phrases(qualifications + responsibilities)
    if not required_skills:
        required_skills = _extract_skill_phrases(lines)

    return JDBreakdown(
        qualifications=qualifications,
        responsibilities=responsibilities,
        required_skills=required_skills,
        preferred_skills=preferred_skills,
        culture_signals=culture_signals,
    )


def calculate_criteria_scores(
    *,
    jd_text: str,
    jd_breakdown: JDBreakdown,
    resume_text: str,
) -> dict[str, float]:
    resume_l = resume_text.lower()

    technical_basis = jd_breakdown.required_skills + jd_breakdown.preferred_skills
    technical = _requirement_match_score(technical_basis, resume_l)

    experience = _experience_depth_score(jd_text, resume_text)
    seniority = _seniority_alignment_score(jd_text, resume_text)

    domain_basis = jd_breakdown.responsibilities or jd_breakdown.qualifications
    domain = _requirement_match_score(domain_basis, resume_l)

    culture = _requirement_match_score(jd_breakdown.culture_signals, resume_l)

    return {
        "technical_skills": technical,
        "experience_depth": experience,
        "seniority_alignment": seniority,
        "domain_relevance": domain,
        "culture_fit": culture,
    }


def compute_overall_score(
    criterion_scores: dict[str, float],
    weights: ScoringWeights | None = None,
) -> float:
    weights = weights or ScoringWeights()
    normalised = weights.normalised()
    total = 0.0
    for key, weight in normalised.items():
        total += float(criterion_scores.get(key, 0.0)) * weight
    return max(0.0, min(1.0, total))


def build_jd_summary(jd_breakdown: JDBreakdown) -> str:
    return (
        f"required={len(jd_breakdown.required_skills)} "
        f"responsibilities={len(jd_breakdown.responsibilities)} "
        f"preferred={len(jd_breakdown.preferred_skills)}"
    )


def load_user_experience_text(payload: dict[str, Any], metadata: dict[str, Any]) -> str:
    direct_text = metadata.get("resume_text")
    if isinstance(direct_text, str) and direct_text.strip():
        return direct_text

    for key in ("tailoring", "tailored", "packaging", "packaged"):
        section = payload.get(key)
        if isinstance(section, dict):
            path_value = section.get("resume_path")
            if isinstance(path_value, str) and path_value.strip():
                content = _read_if_exists(Path(path_value))
                if content:
                    return content

    resume_path = metadata.get("resume_path")
    if isinstance(resume_path, str) and resume_path.strip():
        content = _read_if_exists(Path(resume_path))
        if content:
            return content

    for fallback in (
        Path("resumes/master.md"),
        Path("~/.autorole/resumes/master.md").expanduser(),
    ):
        content = _read_if_exists(fallback)
        if content:
            return content

    profile_payload = metadata.get("user_profile")
    if isinstance(profile_payload, dict):
        return json.dumps(profile_payload, ensure_ascii=True)

    return ""


def _read_if_exists(path: Path) -> str:
    try:
        if path.exists() and path.is_file():
            return path.read_text(encoding="utf-8")
    except Exception:
        return ""
    return ""


def _select_lines(lines: list[str], pattern: str) -> list[str]:
    regex = re.compile(pattern, flags=re.IGNORECASE)
    selected: list[str] = []
    for line in lines:
        if regex.search(line):
            selected.append(line)
    return selected[:40]


def _extract_skill_phrases(lines: list[str]) -> list[str]:
    skill_keywords = {
        "python",
        "java",
        "javascript",
        "typescript",
        "react",
        "node",
        "aws",
        "gcp",
        "azure",
        "docker",
        "kubernetes",
        "sql",
        "postgres",
        "mysql",
        "redis",
        "graphql",
        "rest",
        "api",
        "linux",
        "git",
        "terraform",
        "pandas",
        "spark",
    }
    found: list[str] = []
    for line in lines:
        lowered = line.lower()
        for skill in skill_keywords:
            if skill in lowered and skill not in found:
                found.append(skill)
    return found


def _requirement_match_score(requirements: list[str], resume_text_lower: str) -> float:
    if not requirements:
        return 0.6
    matched = 0
    for requirement in requirements:
        if _phrase_matches_resume(requirement, resume_text_lower):
            matched += 1
    ratio = matched / max(1, len(requirements))
    # Keep a non-zero floor for sparse or noisy JD text.
    return max(0.2, min(1.0, 0.2 + (0.8 * ratio)))


def _phrase_matches_resume(requirement: str, resume_text_lower: str) -> bool:
    raw_tokens = re.findall(r"[a-zA-Z0-9+#.]+", requirement.lower())
    tokens = [token for token in raw_tokens if len(token) >= 3 and token not in _STOP_WORDS]
    if not tokens:
        return False
    hits = sum(1 for token in tokens if token in resume_text_lower)
    required_hits = max(1, int(len(tokens) * 0.5))
    return hits >= required_hits


def _experience_depth_score(jd_text: str, resume_text: str) -> float:
    jd_years = _max_years_requirement(jd_text)
    resume_years = _max_years_requirement(resume_text)
    if jd_years <= 0:
        return 0.6
    if resume_years <= 0:
        return 0.3
    return max(0.0, min(1.0, resume_years / jd_years))


def _seniority_alignment_score(jd_text: str, resume_text: str) -> float:
    jd_level = _detect_seniority_level(jd_text)
    resume_level = _detect_seniority_level(resume_text)
    if jd_level == 0:
        return 0.6
    if resume_level == 0:
        resume_years = _max_years_requirement(resume_text)
        if resume_years >= 8:
            resume_level = 4
        elif resume_years >= 5:
            resume_level = 3
        elif resume_years >= 2:
            resume_level = 2
        else:
            resume_level = 1
    distance = abs(jd_level - resume_level)
    return max(0.2, min(1.0, 1.0 - (distance * 0.2)))


def _max_years_requirement(text: str) -> int:
    matches = re.findall(r"(\d+)\+?\s*years?", text.lower())
    values = [int(value) for value in matches if value.isdigit()]
    return max(values) if values else 0


def _detect_seniority_level(text: str) -> int:
    lowered = text.lower()
    if "principal" in lowered or "distinguished" in lowered:
        return 5
    if "staff" in lowered or "lead" in lowered:
        return 4
    if "senior" in lowered:
        return 3
    if "mid" in lowered or "intermediate" in lowered:
        return 2
    if "junior" in lowered or "entry" in lowered or "associate" in lowered:
        return 1
    return 0


_STOP_WORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "you",
    "your",
    "our",
    "will",
    "have",
    "has",
    "are",
    "not",
    "must",
}
