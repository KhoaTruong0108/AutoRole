from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from autorole_next.config import AppConfig, ScoringConfig
from autorole_next.integrations.llm import AnthropicLLMClient, OllamaLLMClient, OpenAILLMClient

from ..prompting.scoring import SCORE_SYSTEM_PROMPT
from ..scoring_engine import (
    build_jd_summary,
    calculate_criteria_scores,
    compute_overall_score,
    extract_text,
    fetch_jd_html,
    load_user_experience_text,
    parse_jd_breakdown,
)

_CRITERIA_KEYS = (
    "technical_skills",
    "experience_depth",
    "seniority_alignment",
    "domain_relevance",
    "culture_fit",
)

_CRITERIA_LABELS = {
    "technical_skills": "TECHNICAL_SKILLS",
    "experience_depth": "EXPERIENCE_DEPTH",
    "seniority_alignment": "SENIORITY_ALIGNMENT",
    "domain_relevance": "DOMAIN_RELEVANCE",
    "culture_fit": "CULTURE_FIT",
}

_CRITERIA_ALIASES = {
    "TECHNICAL_SKILLS": "technical_skills",
    "EXPERIENCE_DEPTH": "experience_depth",
    "SENIORITY_ALIGNMENT": "seniority_alignment",
    "DOMAIN_RELEVANCE": "domain_relevance",
    "CULTURE_FIT": "culture_fit",
}


class HeuristicScoringStrategy:
    async def score(
        self,
        *,
        payload: dict[str, Any],
        metadata: dict[str, Any],
        config: ScoringConfig,
        app_config: AppConfig,
    ) -> dict[str, Any]:
        _ = config
        _ = app_config

        listing = payload.get("listing") if isinstance(payload.get("listing"), dict) else {}
        job_url = str(listing.get("job_url") or "")
        if not is_valid_http_url(job_url):
            raise ValueError("ScoringExecutor: listing.job_url must be a valid http(s) URL")

        jd_html = str(metadata.get("jd_html") or "")
        if not jd_html:
            page = payload.get("page") if payload.get("page") is not None else metadata.get("page")
            try:
                jd_html = await fetch_jd_html(job_url, page=page)
            except Exception as exc:
                raise ValueError(f"ScoringExecutor: unable to fetch JD HTML from listing.job_url ({job_url}): {exc}") from exc

        jd_text = extract_text(jd_html) if jd_html else ""
        jd_breakdown = parse_jd_breakdown(jd_text)
        resume_text = load_user_experience_text(payload, metadata)

        criteria_scores = calculate_criteria_scores(
            jd_text=jd_text,
            jd_breakdown=jd_breakdown,
            resume_text=resume_text,
        )
        overall = compute_overall_score(criteria_scores, app_config.scoring_weights)
        matched, mismatched = split_matched(criteria_scores)

        return {
            "strategy": "heuristic",
            "overall_score": round(overall, 4),
            "criteria_scores": criteria_scores,
            "matched": matched,
            "mismatched": mismatched,
            "jd_summary": build_jd_summary(jd_breakdown),
            "jd_breakdown": jd_breakdown.model_dump(mode="json"),
        }


class LLMScoringStrategy:
    async def score(
        self,
        *,
        payload: dict[str, Any],
        metadata: dict[str, Any],
        config: ScoringConfig,
        app_config: AppConfig,
    ) -> dict[str, Any]:
        listing = payload.get("listing") if isinstance(payload.get("listing"), dict) else {}
        job_url = str(listing.get("job_url") or "")
        if not is_valid_http_url(job_url):
            raise ValueError("ScoringExecutor: listing.job_url must be a valid http(s) URL")

        jd_html = str(metadata.get("jd_html") or "")
        if not jd_html:
            page = payload.get("page") if payload.get("page") is not None else metadata.get("page")
            try:
                jd_html = await fetch_jd_html(job_url, page=page)
            except Exception as exc:
                raise ValueError(f"ScoringExecutor: unable to fetch JD HTML from listing.job_url ({job_url}): {exc}") from exc

        jd_text_full = extract_text(jd_html) if jd_html else ""
        resume_text_full = load_user_experience_text(payload, metadata)
        jd_text = jd_text_full[: max(0, int(config.llm_max_jd_chars))]
        resume_text = resume_text_full[: max(0, int(config.llm_max_resume_chars))]

        llm = _build_llm_client(app_config)
        response = await llm.call(
            system=SCORE_SYSTEM_PROMPT,
            user=_build_llm_user_prompt(listing, jd_text, resume_text),
            response_model=None,
        )
        parsed = _parse_llm_response(str(response))
        criteria_scores = parsed["criteria_scores"]
        overall = round(compute_overall_score(criteria_scores, app_config.scoring_weights), 4)
        matched, mismatched = split_matched(criteria_scores)
        keywords = [token.strip() for token in parsed["keywords"].split(",") if token.strip()]
        raw_scores = ", ".join(f"{_CRITERIA_LABELS[key]}={int(round(value * 10))}" for key, value in criteria_scores.items())

        return {
            "strategy": "llm",
            "overall_score": overall,
            "criteria_scores": criteria_scores,
            "matched": matched,
            "mismatched": mismatched,
            "jd_summary": f"llm_scores={raw_scores} keywords={len(keywords)}",
            "keywords": keywords,
            "score_reasoning": parsed["reasoning"],
        }


def get_scoring_strategy(strategy_name: str) -> HeuristicScoringStrategy | LLMScoringStrategy:
    normalized = strategy_name.strip().lower()
    if normalized == "heuristic":
        return HeuristicScoringStrategy()
    if normalized == "llm":
        return LLMScoringStrategy()
    raise ValueError(f"ScoringExecutor: unsupported scoring strategy '{strategy_name}'")


def is_valid_http_url(url: str) -> bool:
    if not url.strip():
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def split_matched(criteria_scores: dict[str, float], threshold: float = 0.7) -> tuple[list[str], list[str]]:
    matched = [name for name, value in criteria_scores.items() if value >= threshold]
    mismatched = [name for name, value in criteria_scores.items() if value < threshold]
    return matched, mismatched


def _build_llm_client(config: AppConfig) -> Any:
    provider = str(config.llm.provider).lower()
    if provider == "openai":
        return OpenAILLMClient(config.llm)
    if provider == "anthropic":
        return AnthropicLLMClient(config.llm)
    return OllamaLLMClient(config.llm)


def _build_llm_user_prompt(listing: dict[str, Any], jd_text: str, resume_text: str) -> str:
    title = str(listing.get("job_title") or "Unknown Role")
    company = str(listing.get("company_name") or "Unknown Company")
    platform = str(listing.get("platform") or "")
    return (
        f"RESUME:\n{resume_text}\n\n"
        "---\n\n"
        "JOB POSTING:\n"
        f"TITLE: {title}\n"
        f"COMPANY: {company}\n"
        f"PLATFORM: {platform}\n\n"
        f"DESCRIPTION:\n{jd_text}\n\n"
        "Evaluate the candidate against the requested five criteria. Score each criterion independently based on evidence in the resume, not by averaging from a gut-feel overall score."
    )


def _parse_llm_response(response: str) -> dict[str, Any]:
    criterion_scores_raw: dict[str, int] = {}
    fallback_score: int | None = None
    keywords = ""
    reasoning_lines: list[str] = []
    in_reasoning = False

    for raw_line in response.splitlines():
        line = raw_line.strip()
        upper = line.upper()
        alias = _match_criteria_alias(upper)
        if alias is not None:
            in_reasoning = False
            match = re.search(r"\d+", line)
            if match is not None:
                criterion_scores_raw[alias] = max(1, min(10, int(match.group(0))))
            continue
        if upper.startswith("SCORE:"):
            in_reasoning = False
            match = re.search(r"\d+", line)
            if match is not None:
                fallback_score = max(1, min(10, int(match.group(0))))
            continue
        if upper.startswith("KEYWORDS:"):
            in_reasoning = False
            keywords = line.split(":", maxsplit=1)[1].strip() if ":" in line else ""
            continue
        if upper.startswith("REASONING:"):
            in_reasoning = True
            initial = line.split(":", maxsplit=1)[1].strip() if ":" in line else ""
            if initial:
                reasoning_lines.append(initial)
            continue
        if in_reasoning and line:
            reasoning_lines.append(line)

    if not criterion_scores_raw:
        if fallback_score is None:
            raise ValueError("ScoringExecutor: LLM scoring response is missing criterion scores")
        criterion_scores_raw = {key: fallback_score for key in _CRITERIA_KEYS}

    criteria_scores = {
        key: round(max(1, min(10, criterion_scores_raw.get(key, fallback_score or 1))) / 10.0, 4)
        for key in _CRITERIA_KEYS
    }

    reasoning = " ".join(reasoning_lines).strip() or response.strip()
    return {
        "criteria_scores": criteria_scores,
        "keywords": keywords,
        "reasoning": reasoning,
    }


def _match_criteria_alias(upper_line: str) -> str | None:
    for label, alias in _CRITERIA_ALIASES.items():
        if upper_line.startswith(f"{label}:"):
            return alias
    return None
