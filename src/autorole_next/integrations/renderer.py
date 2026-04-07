from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path

from autorole_next.config import RendererConfig


class ResumeRenderer(ABC):
    @abstractmethod
    async def render(self, md_path: Path, pdf_path: Path) -> None:
        raise NotImplementedError


class PandocRenderer(ResumeRenderer):
    def __init__(
        self,
        pandoc_path: str = "pandoc",
        template: str = "",
        *,
        font_size_pt: float = 9.5,
        line_height: float = 1.18,
        page_margin_in: float = 0.4,
    ) -> None:
        self._pandoc = pandoc_path
        self._template = template
        self._font_size_pt = font_size_pt
        self._line_height = line_height
        self._page_margin_in = page_margin_in

    async def render(self, md_path: Path, pdf_path: Path) -> None:
        cmd = [
            self._pandoc,
            str(md_path),
            "-o",
            str(pdf_path),
            "--pdf-engine=xelatex",
            "-V",
            f"fontsize={self._font_size_pt}pt",
            "-V",
            f"geometry:margin={self._page_margin_in}in",
            "-V",
            f"linestretch={self._line_height}",
        ]
        if self._template:
            cmd.extend(["--template", self._template])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Pandoc failed: {stderr.decode('utf-8', errors='replace')}")


class WeasyPrintRenderer(ResumeRenderer):
    def __init__(
        self,
        *,
        font_size_pt: float = 9.5,
        line_height: float = 1.18,
        page_margin_in: float = 0.4,
    ) -> None:
        self._font_size_pt = font_size_pt
        self._line_height = line_height
        self._page_margin_in = page_margin_in

    async def render(self, md_path: Path, pdf_path: Path) -> None:
        import markdown
        from weasyprint import CSS, HTML

        html = markdown.markdown(md_path.read_text(encoding="utf-8"))
        stylesheet = CSS(
            string=_compact_resume_css(
                font_size_pt=self._font_size_pt,
                line_height=self._line_height,
                page_margin_in=self._page_margin_in,
            )
        )
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: HTML(string=html).write_pdf(str(pdf_path), stylesheets=[stylesheet]))


def build_renderer(config: RendererConfig) -> ResumeRenderer:
    if config.engine == "pandoc":
        return PandocRenderer(
            pandoc_path=config.pandoc_path,
            template=config.template,
            font_size_pt=config.font_size_pt,
            line_height=config.line_height,
            page_margin_in=config.page_margin_in,
        )
    return WeasyPrintRenderer(
        font_size_pt=config.font_size_pt,
        line_height=config.line_height,
        page_margin_in=config.page_margin_in,
    )


def _compact_resume_css(*, font_size_pt: float, line_height: float, page_margin_in: float) -> str:
    return f"""
    @page {{
        margin: {page_margin_in}in;
    }}

    html {{
        font-size: {font_size_pt}pt;
    }}

    body {{
        font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif;
        font-size: {font_size_pt}pt;
        line-height: {line_height};
        color: #111827;
    }}

    h1, h2, h3, h4 {{
        line-height: 1.08;
        margin: 0.45em 0 0.2em;
    }}

    h1 {{ font-size: {font_size_pt * 1.45:.2f}pt; }}
    h2 {{ font-size: {font_size_pt * 1.2:.2f}pt; }}
    h3 {{ font-size: {font_size_pt * 1.05:.2f}pt; }}

    p, ul, ol {{
        margin: 0.18em 0 0.32em;
    }}

    ul, ol {{
        padding-left: 1.05rem;
    }}

    li {{
        margin: 0.08em 0;
    }}

    strong {{
        font-weight: 600;
    }}

    hr {{
        margin: 0.45em 0;
    }}
    """