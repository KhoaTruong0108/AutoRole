from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_text(value: str) -> str:
    return " ".join(value.strip().lower().split())


def normalize_url(url: str) -> str:
    raw = url.strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    normalized_query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    normalized_path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), normalized_path, normalized_query, ""))


class ListingPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_url: str
    apply_url: str | None = None
    company_name: str
    job_title: str
    platform: str
    external_job_id: str | None = None


def canonical_listing_key(listing: ListingPayload) -> str:
    normalized_parts = [
        _normalize_text(listing.platform),
        _normalize_text(listing.company_name),
        _normalize_text(listing.job_title),
        _normalize_text(listing.external_job_id or ""),
        normalize_url(listing.job_url),
        normalize_url(listing.apply_url or ""),
    ]
    return "|".join(normalized_parts)


def correlation_id_for_listing(listing: ListingPayload) -> str:
    key = canonical_listing_key(listing)
    digest = sha256(key.encode("utf-8")).hexdigest()[:12]
    return f"listing-{uuid5(NAMESPACE_URL, key)}-{digest}"


class ListingSeed(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listing: ListingPayload
    source_name: str
    discovered_at: datetime = Field(default_factory=_utcnow)
    source_metadata: dict[str, Any] = Field(default_factory=dict)


class ExplorationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_filters: dict[str, Any] = Field(default_factory=dict)
    job_url: str = ""
    job_urls_file: str = ""
    platform_hint: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SeedRunPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    listing: ListingPayload
    source_name: str
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    discovered_at: datetime
    canonical_key: str


class SeededRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    correlation_id: str
    canonical_key: str
    status: Literal["seeded", "duplicate"]
    source_name: str
