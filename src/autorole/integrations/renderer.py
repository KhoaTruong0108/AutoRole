from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path


class ResumeRenderer(ABC):
	@abstractmethod
	async def render(self, md_path: Path, pdf_path: Path) -> None:
		"""Render markdown resume into PDF file."""


class PandocRenderer(ResumeRenderer):
	"""Render markdown to PDF through Pandoc + XeLaTeX."""

	def __init__(self, pandoc_path: str = "pandoc", template: str = "") -> None:
		self._pandoc = pandoc_path
		self._template = template

	async def render(self, md_path: Path, pdf_path: Path) -> None:
		cmd = [
			self._pandoc,
			str(md_path),
			"-o",
			str(pdf_path),
			"--pdf-engine=xelatex",
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
	"""Render markdown to HTML then to PDF via WeasyPrint."""

	async def render(self, md_path: Path, pdf_path: Path) -> None:
		import markdown
		from weasyprint import HTML

		html = markdown.markdown(md_path.read_text(encoding="utf-8"))
		loop = asyncio.get_running_loop()
		await loop.run_in_executor(None, lambda: HTML(string=html).write_pdf(str(pdf_path)))

