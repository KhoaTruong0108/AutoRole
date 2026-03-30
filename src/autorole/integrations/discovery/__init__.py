from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from autorole.integrations.discovery.jobspy import JobSpyDiscoveryProvider
from autorole.integrations.discovery.normalization import canonical_listing_key, generate_run_id, normalize_listing
from autorole.integrations.discovery.smartextract import SmartExtractDiscoveryProvider
from autorole.integrations.discovery.workday import WorkdayDiscoveryProvider
from autorole.integrations.scrapers.base import JobDiscoveryProvider


def build_discovery_providers(
	platforms: Sequence[str] | None = None,
	*,
	llm_client: Any | None = None,
	render_html: Any | None = None,
) -> dict[str, JobDiscoveryProvider]:
	selected = {platform.strip().lower() for platform in (platforms or []) if platform.strip()}
	providers: dict[str, JobDiscoveryProvider] = {}
	if "jobspy" in selected:
		providers["jobspy"] = JobSpyDiscoveryProvider()
	if "smartextract" in selected:
		providers["smartextract"] = SmartExtractDiscoveryProvider(
			llm_client=llm_client,
			render_html=render_html,
		)
	if "workday" in selected:
		providers["workday"] = WorkdayDiscoveryProvider()
	return providers


__all__ = [
	"JobDiscoveryProvider",
	"JobSpyDiscoveryProvider",
	"SmartExtractDiscoveryProvider",
	"WorkdayDiscoveryProvider",
	"canonical_listing_key",
	"build_discovery_providers",
	"generate_run_id",
	"normalize_listing",
]