from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse, urlunparse

from autorole.context import JobListing


def normalize_listing(listing: JobListing) -> JobListing:
	return listing.model_copy(
		update={
			"job_url": normalize_url(listing.job_url),
			"apply_url": normalize_url(listing.apply_url),
			"company_name": _normalize_text(listing.company_name),
			"job_id": _normalize_text(listing.job_id),
			"job_title": _normalize_text(listing.job_title),
			"platform": _normalize_text(listing.platform).lower(),
		}
	)


def canonical_listing_key(listing: JobListing) -> str:
	normalized = normalize_listing(listing)
	company = _slug_token(normalized.company_name)
	job_id = _slug_token(normalized.job_id)
	if company and job_id:
		return f"{company}:{job_id}"

	apply_url = normalized.apply_url.strip()
	if apply_url:
		return f"url:{apply_url}"

	job_url = normalized.job_url.strip()
	if job_url:
		return f"url:{job_url}"

	title = _slug_token(normalized.job_title)
	platform = _slug_token(normalized.platform)
	return f"fallback:{company}:{job_id}:{title}:{platform}"


def generate_run_id(listing: JobListing) -> str:
	normalized = normalize_listing(listing)
	company = _slug_token(normalized.company_name)
	job_id = _slug_token(normalized.job_id)
	if company and job_id:
		return f"{company}_{job_id}"

	digest = hashlib.sha1(canonical_listing_key(normalized).encode("utf-8")).hexdigest()[:12]
	prefix = company or _slug_token(normalized.platform) or "listing"
	return f"{prefix}_{digest}"


def normalize_url(value: str) -> str:
	url = value.strip()
	if not url:
		return ""

	parsed = urlparse(url)
	path = parsed.path or "/"
	if path != "/":
		path = path.rstrip("/") or "/"
	host = parsed.netloc.lower()
	scheme = parsed.scheme.lower() or "https"
	return urlunparse(parsed._replace(scheme=scheme, netloc=host, path=path, fragment=""))


def _normalize_text(value: str) -> str:
	return re.sub(r"\s+", " ", value.strip())


def _slug_token(value: str) -> str:
	text = _normalize_text(value).lower()
	text = re.sub(r"[^a-z0-9]+", "_", text)
	return text.strip("_")