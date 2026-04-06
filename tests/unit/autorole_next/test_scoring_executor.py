from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from autorole_next._snapflow import StateContext
from autorole_next.executors.scoring import ScoringExecutor
from autorole_next.scoring import strategies as scoring_strategies


@dataclass
class _FakeStore:
    calls: list[dict[str, object]]

    async def append_score_report(
        self,
        correlation_id: str,
        *,
        attempt: int,
        overall_score: float,
        criteria_scores: dict[str, float],
        matched: list[str],
        mismatched: list[str],
        jd_summary: str,
    ) -> None:
        self.calls.append(
            {
                "correlation_id": correlation_id,
                "attempt": attempt,
                "overall_score": overall_score,
                "criteria_scores": criteria_scores,
                "matched": matched,
                "mismatched": mismatched,
                "jd_summary": jd_summary,
            }
        )


def _ctx(data: dict[str, object], metadata: dict[str, object] | None = None) -> StateContext[dict[str, object]]:
    return StateContext[dict[str, object]](
        correlation_id="score-corr-1",
        current_stage="scoring",
        data=data,
        metadata={} if metadata is None else metadata,
    )


def test_scoring_executor_keeps_forced_score_behavior() -> None:
    store = _FakeStore(calls=[])
    ScoringExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = ScoringExecutor()

    result = asyncio.run(
        executor.execute(
            _ctx(
                {
                    "listing": {
                        "job_url": "https://example.com/jobs/forced",
                        "company_name": "Acme",
                        "job_title": "Engineer",
                        "platform": "workday",
                    }
                },
                metadata={"forced_score": 0.93},
            )
        )
    )

    assert result.success is True
    scoring = dict(result.data).get("scoring")
    assert isinstance(scoring, dict)
    assert float(scoring["overall_score"]) >= 0.9
    assert "jd_html" not in scoring
    assert len(store.calls) == 1


def test_scoring_executor_calculates_from_jd_and_resume_mapping() -> None:
    store = _FakeStore(calls=[])
    ScoringExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = ScoringExecutor()

    jd_html = """
    <html><body>
      <h1>Senior Backend Engineer</h1>
      <p>Requirements: 5+ years experience, Python, AWS, Docker, SQL</p>
      <p>Responsibilities: build APIs, maintain distributed systems, mentor team</p>
      <p>Preferred: Kubernetes</p>
      <p>Culture: collaborative and mission-driven</p>
    </body></html>
    """
    resume_text = """
    Senior software engineer with 7 years building Python APIs on AWS.
    Built Docker-based services, SQL data models, and Kubernetes workloads.
    Mentor teammates and collaborate across product and platform teams.
    """

    result = asyncio.run(
        executor.execute(
            _ctx(
                {
                    "listing": {
                        "job_url": "https://example.com/jobs/match",
                        "company_name": "Acme",
                        "job_title": "Senior Backend Engineer",
                        "platform": "workday",
                    }
                },
                metadata={"jd_html": jd_html, "resume_text": resume_text},
            )
        )
    )

    assert result.success is True
    scoring = dict(result.data).get("scoring")
    assert isinstance(scoring, dict)

    criteria = scoring.get("criteria_scores")
    assert isinstance(criteria, dict)
    assert float(criteria.get("technical_skills", 0.0)) >= 0.7
    assert float(criteria.get("experience_depth", 0.0)) >= 0.7
    assert float(scoring.get("overall_score", 0.0)) >= 0.7
    assert "jd_html" not in scoring

    assert len(store.calls) == 1
    assert store.calls[0]["jd_summary"] != ""


def test_scoring_executor_fails_on_invalid_listing_url() -> None:
    store = _FakeStore(calls=[])
    ScoringExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = ScoringExecutor()

    result = asyncio.run(
        executor.execute(
            _ctx(
                {
                    "listing": {
                        "job_url": "notaurl",
                        "company_name": "Acme",
                        "job_title": "Engineer",
                        "platform": "workday",
                    }
                },
                metadata={"resume_text": "Python engineer"},
            )
        )
    )

    assert result.success is False
    assert "valid http(s) URL" in str(result.error)
    assert len(store.calls) == 0


def test_scoring_executor_uses_llm_strategy_from_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore(calls=[])
    ScoringExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = ScoringExecutor()

    class _FakeLLM:
        async def call(self, system: str, user: str, response_model=None, temperature=None) -> str:  # noqa: ANN001
            return "SCORE: 8\nKEYWORDS: python,aws,docker\nREASONING: Strong backend fit with cloud experience."

    monkeypatch.setattr(scoring_strategies, "_build_llm_client", lambda _cfg: _FakeLLM())

    result = asyncio.run(
        executor.execute(
            _ctx(
                {
                    "listing": {
                        "job_url": "https://example.com/jobs/llm",
                        "company_name": "Acme",
                        "job_title": "Backend Engineer",
                        "platform": "workday",
                    }
                },
                metadata={
                    "scoring_strategy": "llm",
                    "jd_html": "<html><body><h1>Backend Engineer</h1><p>Python AWS Docker</p></body></html>",
                    "resume_text": "Engineer with Python and AWS delivery experience.",
                },
            )
        )
    )

    assert result.success is True
    scoring = dict(result.data).get("scoring")
    assert isinstance(scoring, dict)
    assert scoring.get("strategy") == "llm"
    assert float(scoring.get("overall_score", 0.0)) == 0.8
    assert scoring.get("keywords") == ["python", "aws", "docker"]
    assert "score_reasoning" in scoring
    assert len(store.calls) == 1


def test_scoring_executor_uses_scoring_config_strategy(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore(calls=[])
    ScoringExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = ScoringExecutor()

    class _FakeLLM:
        async def call(self, system: str, user: str, response_model=None, temperature=None) -> str:  # noqa: ANN001
            return "SCORE: 7\nKEYWORDS: sql,api\nREASONING: Solid platform and API background."

    monkeypatch.setattr(scoring_strategies, "_build_llm_client", lambda _cfg: _FakeLLM())

    result = asyncio.run(
        executor.execute(
            _ctx(
                {
                    "listing": {
                        "job_url": "https://example.com/jobs/llm-config",
                        "company_name": "Acme",
                        "job_title": "Platform Engineer",
                        "platform": "workday",
                    }
                },
                metadata={
                    "scoring_config": {
                        "strategy": "llm",
                        "llm_max_jd_chars": 2000,
                        "llm_max_resume_chars": 2000,
                    },
                    "jd_html": "<html><body><p>SQL APIs reliability</p></body></html>",
                    "resume_text": "Built API services and data platforms.",
                },
            )
        )
    )

    assert result.success is True
    scoring = dict(result.data).get("scoring")
    assert isinstance(scoring, dict)
    assert scoring.get("strategy") == "llm"
    assert float(scoring.get("overall_score", 0.0)) == 0.7
    assert len(store.calls) == 1
