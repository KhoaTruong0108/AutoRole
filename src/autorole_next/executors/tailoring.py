from __future__ import annotations

from pathlib import Path
from typing import Any

from autorole_next.config import AppConfig, TailoringConfig
from autorole_next.integrations.llm import AnthropicLLMClient, LLMResponseError, OllamaLLMClient, OpenAILLMClient

from .._snapflow import Executor, StageResult, StateContext
from ..prompting.tailoring import TAILORING_SYSTEM_PROMPTS
from ..store import AutoRoleStoreAdapter
from ..tailoring_engine import (
    build_diff_summary,
    build_resume_path,
    resolve_source_resume,
    select_degree_for_attempt,
    tailor_resume,
    utcnow_iso,
)


def _utcnow_iso() -> str:
    return utcnow_iso()

class TailoringExecutor(Executor[dict[str, Any]]):
    _store: AutoRoleStoreAdapter | None = None

    @classmethod
    def configure_store(cls, store: AutoRoleStoreAdapter) -> None:
        cls._store = store

    async def execute(self, ctx: StateContext[dict[str, Any]]) -> StageResult[dict[str, Any]]:
        payload = dict(ctx.data)
        metadata = dict(ctx.metadata)
        scoring = payload.get("scoring") if isinstance(payload.get("scoring"), dict) else {}

        if not scoring:
            return StageResult.fail("TailoringExecutor: scoring payload is required", "PreconditionError")

        overall = float(scoring.get("overall_score", 0.0))
        attempt = int(scoring.get("attempt", int(ctx.attempt) + 1))
        app_config = AppConfig()
        tailoring_override = metadata.get("tailoring_config")
        if isinstance(tailoring_override, TailoringConfig):
            tailoring_config = tailoring_override
        elif isinstance(tailoring_override, dict):
            try:
                tailoring_config = TailoringConfig.model_validate(tailoring_override)
            except Exception:
                tailoring_config = app_config.tailoring
        else:
            tailoring_config = app_config.tailoring

        degree = select_degree_for_attempt(overall, attempt=attempt, config=tailoring_config)

        source_md, parent_resume_id, version = resolve_source_resume(payload, metadata)
        use_llm = bool(metadata.get("tailoring_use_llm", True))
        if degree == 0:
            tailored_md = source_md
        elif use_llm:
            try:
                llm = _build_llm_client(app_config)
                response = await llm.call(
                    system=TAILORING_SYSTEM_PROMPTS.get(degree, TAILORING_SYSTEM_PROMPTS[3]),
                    user=_build_tailoring_prompt(source_md, scoring, degree),
                    response_model=None,
                )
                tailored_md = str(response).strip() or source_md
            except LLMResponseError as exc:
                return StageResult.fail(str(exc), "LLMResponseError")
            except Exception as exc:
                return StageResult.fail(f"Tailoring call failed: {exc}", "TailoringError")
        else:
            tailored_md = tailor_resume(source_md, degree=degree, scoring=scoring)

        resume_path = build_resume_path(ctx.correlation_id, version, metadata)
        self._materialize_tailored_resume(
            resume_path,
            content=tailored_md,
        )

        diff_summary = build_diff_summary(
            source_md=source_md,
            tailored_md=tailored_md,
            degree=degree,
            scoring=scoring,
        )

        resume_id = f"resume-{ctx.correlation_id[:12]}-v{version}"

        tailoring_payload = {
            "tailoring_degree": degree,
            "resume_id": resume_id,
            "parent_resume_id": parent_resume_id,
            "resume_path": resume_path,
            "diff_summary": diff_summary,
            "tailored_at": _utcnow_iso(),
        }
        payload["tailoring"] = tailoring_payload

        store = self._store
        if store is None:
            raise RuntimeError("TailoringExecutor store is not configured")

        await store.append_tailored_resume(
            ctx.correlation_id,
            attempt=attempt,
            resume_path=resume_path,
            diff_summary=diff_summary,
            tailoring_degree=degree,
        )
        return StageResult.ok(payload)

    @staticmethod
    def _materialize_tailored_resume(
        resume_path: str,
        *,
        content: str,
    ) -> None:
        target = Path(resume_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.rstrip() + "\n", encoding="utf-8")

def _build_llm_client(config: AppConfig) -> Any:
    provider = str(config.llm.provider).lower()
    if provider == "openai":
        return OpenAILLMClient(config.llm)
    if provider == "anthropic":
        return AnthropicLLMClient(config.llm)
    return OllamaLLMClient(config.llm)


def _build_tailoring_prompt(source_md: str, score: dict[str, Any], degree: int) -> str:
    mismatched = score.get("mismatched") if isinstance(score.get("mismatched"), list) else []
    return (
        f"Tailoring degree: {degree}\n"
        f"Current overall score: {float(score.get('overall_score', 0.0)):.4f}\n"
        f"Mismatched criteria: {', '.join(str(item) for item in mismatched) if mismatched else 'none'}\n\n"
        "Source resume markdown:\n"
        f"{source_md}\n\n"
        "Return only the fully revised markdown resume."
    )