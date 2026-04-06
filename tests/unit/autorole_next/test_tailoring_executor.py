from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

from autorole_next._snapflow import StateContext
from autorole_next.executors.tailoring import TailoringExecutor


@dataclass
class _FakeStore:
    calls: list[dict[str, object]]

    async def append_tailored_resume(
        self,
        correlation_id: str,
        *,
        attempt: int,
        resume_path: str,
        diff_summary: str,
        tailoring_degree: int,
    ) -> None:
        self.calls.append(
            {
                "correlation_id": correlation_id,
                "attempt": attempt,
                "resume_path": resume_path,
                "diff_summary": diff_summary,
                "tailoring_degree": tailoring_degree,
            }
        )


def _ctx(data: dict[str, object], metadata: dict[str, object] | None = None) -> StateContext[dict[str, object]]:
    return StateContext[dict[str, object]](
        correlation_id="tailor-corr-1",
        current_stage="tailoring",
        data=data,
        metadata={} if metadata is None else metadata,
    )


def test_tailoring_executor_requires_scoring_payload() -> None:
    store = _FakeStore(calls=[])
    TailoringExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = TailoringExecutor()

    result = asyncio.run(executor.execute(_ctx({"listing": {}})))

    assert result.success is False
    assert result.error_type == "PreconditionError"


def test_tailoring_executor_degree_zero_for_high_score() -> None:
    store = _FakeStore(calls=[])
    TailoringExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = TailoringExecutor()

    result = asyncio.run(
        executor.execute(
            _ctx(
                {
                    "scoring": {
                        "attempt": 1,
                        "overall_score": 0.93,
                        "criteria_scores": {
                            "technical_skills": 0.95,
                            "experience_depth": 0.91,
                            "seniority_alignment": 0.90,
                            "domain_relevance": 0.88,
                            "culture_fit": 0.85,
                        },
                        "matched": ["technical_skills"],
                        "mismatched": [],
                        "jd_breakdown": {"required_skills": ["python", "aws"]},
                    }
                },
                metadata={"resume_text": "Senior engineer with Python and AWS experience."},
            )
        )
    )

    assert result.success is True
    output = dict(result.data)
    tailoring = output.get("tailoring")
    assert isinstance(tailoring, dict)
    assert int(tailoring["tailoring_degree"]) == 0
    assert len(store.calls) == 1


def test_tailoring_executor_writes_diff_summary_json() -> None:
    store = _FakeStore(calls=[])
    TailoringExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = TailoringExecutor()

    result = asyncio.run(
        executor.execute(
            _ctx(
                {
                    "scoring": {
                        "attempt": 2,
                        "overall_score": 0.35,
                        "criteria_scores": {
                            "technical_skills": 0.3,
                            "experience_depth": 0.32,
                            "seniority_alignment": 0.34,
                            "domain_relevance": 0.31,
                            "culture_fit": 0.33,
                        },
                        "matched": [],
                        "mismatched": ["technical_skills", "domain_relevance"],
                        "jd_breakdown": {"required_skills": ["python", "kubernetes", "aws"]},
                    },
                    "tailoring": {
                        "resume_id": "resume-old",
                        "resume_path": "resumes/tailor-corr-1/tailored_v1.md",
                    },
                },
                metadata={"resume_text": "Backend engineer with APIs and SQL.", "tailoring_use_llm": False},
            )
        )
    )

    assert result.success is True
    output = dict(result.data)
    tailoring = output.get("tailoring")
    assert isinstance(tailoring, dict)
    assert int(tailoring["tailoring_degree"]) >= 1

    parsed = json.loads(str(tailoring["diff_summary"]))
    assert "tailoring_degree" in parsed
    assert "sections" in parsed
    assert len(store.calls) == 1


def test_tailoring_executor_uses_llm_for_non_zero_degree() -> None:
    store = _FakeStore(calls=[])
    TailoringExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = TailoringExecutor()

    llm = AsyncMock()
    llm.call = AsyncMock(return_value="# Revised Resume\n\n- impact line\n")

    with patch("autorole_next.executors.tailoring._build_llm_client", return_value=llm):
        result = asyncio.run(
            executor.execute(
                _ctx(
                    {
                        "scoring": {
                            "attempt": 1,
                            "overall_score": 0.45,
                            "criteria_scores": {
                                "technical_skills": 0.4,
                                "experience_depth": 0.44,
                                "seniority_alignment": 0.46,
                                "domain_relevance": 0.42,
                                "culture_fit": 0.43,
                            },
                            "matched": [],
                            "mismatched": ["technical_skills"],
                            "jd_breakdown": {"required_skills": ["python"]},
                        }
                    },
                    metadata={"resume_text": "Base resume text.", "tailoring_use_llm": True},
                )
            )
        )

    assert result.success is True
    assert llm.call.await_count == 1
    output = dict(result.data)
    tailoring = output.get("tailoring")
    assert isinstance(tailoring, dict)
    assert int(tailoring["tailoring_degree"]) >= 1


def test_tailoring_executor_escalates_degree_with_attempt() -> None:
    store = _FakeStore(calls=[])
    TailoringExecutor.configure_store(store)  # type: ignore[arg-type]
    executor = TailoringExecutor()

    result = asyncio.run(
        executor.execute(
            _ctx(
                {
                    "scoring": {
                        "attempt": 2,
                        "overall_score": 0.75,
                        "criteria_scores": {
                            "technical_skills": 0.75,
                            "experience_depth": 0.75,
                            "seniority_alignment": 0.75,
                            "domain_relevance": 0.75,
                            "culture_fit": 0.75,
                        },
                        "matched": ["technical_skills"],
                        "mismatched": ["domain_relevance"],
                        "jd_breakdown": {"required_skills": ["python"]},
                    }
                },
                metadata={"resume_text": "Senior engineer with Python and AWS experience.", "tailoring_use_llm": False},
            )
        )
    )

    assert result.success is True
    output = dict(result.data)
    tailoring = output.get("tailoring")
    assert isinstance(tailoring, dict)
    # Base degree for 0.75 is 1, retry attempt=2 should escalate by one level.
    assert int(tailoring["tailoring_degree"]) == 2
