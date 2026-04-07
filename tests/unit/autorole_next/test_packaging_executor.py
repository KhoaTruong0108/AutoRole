from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from autorole_next._snapflow import StateContext
from autorole_next.executors.packaging import PackagingExecutor


@dataclass
class _FakeStore:
    calls: list[dict[str, object]]

    async def upsert_application_packaging(
        self,
        correlation_id: str,
        *,
        resume_path: str,
        pdf_path: str,
    ) -> None:
        self.calls.append(
            {
                "correlation_id": correlation_id,
                "resume_path": resume_path,
                "pdf_path": pdf_path,
            }
        )


class _FakeRenderer:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    async def render(self, md_path: Path, pdf_path: Path) -> None:
        self.calls.append((md_path, pdf_path))
        pdf_path.write_bytes(b"%PDF-1.4\n% rendered\n")


def _ctx(data: dict[str, object]) -> StateContext[dict[str, object]]:
    return StateContext[dict[str, object]](
        correlation_id="corr-package-1",
        current_stage="packaging",
        data=data,
    )


def test_packaging_executor_requires_tailoring_payload(tmp_path: Path) -> None:
    store = _FakeStore(calls=[])
    renderer = _FakeRenderer()
    PackagingExecutor.configure_store(store)  # type: ignore[arg-type]
    PackagingExecutor.configure_renderer(renderer)  # type: ignore[arg-type]
    executor = PackagingExecutor()

    result = asyncio.run(executor.execute(_ctx({})))

    assert result.success is False
    assert result.error_type == "PreconditionError"


def test_packaging_executor_renders_pdf_from_tailoring_output(tmp_path: Path) -> None:
    resume_path = tmp_path / "Alex_Nguyen_resume_2.md"
    resume_path.write_text("# Tailored Resume\n\nBuilt from markdown.\n", encoding="utf-8")

    store = _FakeStore(calls=[])
    renderer = _FakeRenderer()
    PackagingExecutor.configure_store(store)  # type: ignore[arg-type]
    PackagingExecutor.configure_renderer(renderer)  # type: ignore[arg-type]
    executor = PackagingExecutor()

    result = asyncio.run(
        executor.execute(
            _ctx(
                {
                    "tailoring": {
                        "resume_id": "resume-1",
                        "resume_path": str(resume_path),
                    }
                }
            )
        )
    )

    assert result.success is True
    data = dict(result.data)
    packaging = data.get("packaging")
    assert isinstance(packaging, dict)
    assert "packaged" not in data
    assert packaging.get("resume_id") == "resume-1"
    assert packaging.get("resume_path") == str(resume_path)
    assert packaging.get("pdf_path") == str(tmp_path / "Alex_Nguyen_resume.pdf")
    assert Path(str(packaging.get("pdf_path"))).read_bytes().startswith(b"%PDF-1.4")
    assert renderer.calls == [(resume_path, tmp_path / "Alex_Nguyen_resume.pdf")]
    assert store.calls == [
        {
            "correlation_id": "corr-package-1",
            "resume_path": str(resume_path),
            "pdf_path": str(tmp_path / "Alex_Nguyen_resume.pdf"),
        }
    ]


def test_packaging_executor_fails_for_empty_tailored_resume(tmp_path: Path) -> None:
    resume_path = tmp_path / "Alex_Nguyen_resume_3.md"
    resume_path.write_text("\n\n", encoding="utf-8")

    store = _FakeStore(calls=[])
    renderer = _FakeRenderer()
    PackagingExecutor.configure_store(store)  # type: ignore[arg-type]
    PackagingExecutor.configure_renderer(renderer)  # type: ignore[arg-type]
    executor = PackagingExecutor()

    result = asyncio.run(executor.execute(_ctx({"tailoring": {"resume_path": str(resume_path)}})))

    assert result.success is False
    assert result.error_type == "PreconditionError"
    assert store.calls == []
    assert renderer.calls == []


def test_packaging_executor_overwrites_stable_pdf_name(tmp_path: Path) -> None:
    resume_path = tmp_path / "Alex_Nguyen_resume_4.md"
    resume_path.write_text("# Tailored Resume\n", encoding="utf-8")
    pdf_path = tmp_path / "Alex_Nguyen_resume.pdf"
    pdf_path.write_bytes(b"stale")

    store = _FakeStore(calls=[])
    renderer = _FakeRenderer()
    PackagingExecutor.configure_store(store)  # type: ignore[arg-type]
    PackagingExecutor.configure_renderer(renderer)  # type: ignore[arg-type]
    executor = PackagingExecutor()

    result = asyncio.run(executor.execute(_ctx({"tailoring": {"resume_path": str(resume_path)}})))

    assert result.success is True
    assert pdf_path.read_bytes().startswith(b"%PDF-1.4")