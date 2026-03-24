from __future__ import annotations

from typing import Any, Protocol


class FormExtractor(Protocol):
    async def extract(self, page: Any) -> dict[str, Any]:
        """Extract form fields from the current page context."""


class FormApplier(Protocol):
    async def fill(self, page: Any, form_json_filled: dict[str, Any]) -> None:
        """Fill form fields on the current page."""

    async def attach_resume(self, page: Any, file_path: str) -> None:
        """Attach resume or supporting file to the form."""

    async def submit(self, page: Any) -> None:
        """Submit the form."""

    async def confirm(self, page: Any) -> bool:
        """Return whether the submission appears successful."""
