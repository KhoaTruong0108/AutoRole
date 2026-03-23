"""ATS scraper registry and helpers."""

from __future__ import annotations

from typing import Any

from autorole.integrations.scrapers.base import ATSScraper
from autorole.integrations.scrapers.detection import detect_ats
from autorole.integrations.scrapers.generic import GenericScraper

try:
	from autorole.integrations.scrapers.lever import LeverScraper
except Exception:  # pragma: no cover - defensive import fallback
	LeverScraper = None  # type: ignore[assignment]

try:
	from autorole.integrations.scrapers.greenhouse import GreenhouseScraper
except Exception:  # pragma: no cover - defensive import fallback
	GreenhouseScraper = None  # type: ignore[assignment]

_REGISTRY: dict[str, type[ATSScraper]] = {
	"generic": GenericScraper,
}

if LeverScraper is not None:
	_REGISTRY["lever"] = LeverScraper
if GreenhouseScraper is not None:
	_REGISTRY["greenhouse"] = GreenhouseScraper


def register_scraper(ats: str, scraper_cls: type[ATSScraper]) -> None:
	"""Register or override ATS scraper implementation at runtime."""
	_REGISTRY[ats] = scraper_cls


def get_scraper(url: str, page: Any | None = None) -> ATSScraper:
	"""Return ATS scraper instance inferred from URL."""
	ats = detect_ats(url)
	cls = _REGISTRY.get(ats, GenericScraper)
	return cls(page=page)


__all__ = ["ATSScraper", "detect_ats", "get_scraper", "register_scraper"]

