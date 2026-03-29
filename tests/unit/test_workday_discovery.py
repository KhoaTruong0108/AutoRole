from __future__ import annotations

import httpx

from autorole.config import SearchFilter
from autorole.integrations.discovery import build_discovery_providers
from autorole.integrations.discovery.workday import (
	DEFAULT_WORKDAY_EMPLOYERS,
	WorkdayDiscoveryProvider,
	WorkdayEmployer,
	_describe_workday_error,
	_extract_job_id,
	load_workday_employers,
	_matches_location,
	_title_excluded,
)


async def test_workday_provider_returns_empty_without_keywords() -> None:
	provider = WorkdayDiscoveryProvider(employers={})

	results = await provider.search(SearchFilter(platforms=["workday"], keywords=[], location=""))

	assert results == []


def test_build_discovery_providers_includes_workday() -> None:
	providers = build_discovery_providers(["linkedin", "workday"])

	assert "workday" in providers
	assert isinstance(providers["workday"], WorkdayDiscoveryProvider)


def test_load_workday_employers_reads_yaml_registry() -> None:
	employers = load_workday_employers()

	assert employers
	assert employers["salesforce"].site_id == "External_Career_Site"
	assert employers["servicenow"].site_id == "ServiceNowCareers"
	assert employers["docusign"].site_id == "External"
	assert employers["netflix"].site_id == "Netflix"


def test_default_workday_employers_loaded_from_yaml() -> None:
	assert DEFAULT_WORKDAY_EMPLOYERS["paypal"].site_id == "jobs"


def test_workday_provider_normalizes_posting() -> None:
	provider = WorkdayDiscoveryProvider(
		employers={
			"acme": WorkdayEmployer(
				name="Acme",
				tenant="acme",
				site_id="Careers",
				base_url="https://acme.wd5.myworkdayjobs.com",
			),
		},
	)
	employer = provider._employers["acme"]
	posting = {
		"title": "Backend Engineer",
		"externalPath": "/job/Toronto-ON/Backend-Engineer_JR-42",
	}

	listing = provider._to_listing(employer, posting)

	assert listing is not None
	assert listing.job_url == "https://acme.wd5.myworkdayjobs.com/Careers/job/Toronto-ON/Backend-Engineer_JR-42"
	assert listing.apply_url == listing.job_url
	assert listing.company_name == "Acme"
	assert listing.job_id == "Backend-Engineer_JR-42"
	assert listing.platform == "workday"


def test_workday_location_matching_accepts_remote_and_target_city() -> None:
	assert _matches_location("Toronto, ON", "Toronto, Ontario, Canada")
	assert _matches_location("Toronto, ON", "Remote - Canada")
	assert not _matches_location("Toronto, ON", "Austin, Texas, United States")


def test_workday_title_exclusion_matches_case_insensitively() -> None:
	assert _title_excluded(["senior director", "intern"], "Senior Director, Platform")
	assert not _title_excluded(["intern"], "Backend Engineer")


def test_extract_job_id_uses_path_tail() -> None:
	assert _extract_job_id("/job/Toronto-ON/Backend-Engineer_JR-42") == "Backend-Engineer_JR-42"


def test_describe_workday_error_includes_http_status_and_error_code() -> None:
	request = httpx.Request("POST", "https://example.com/jobs")
	response = httpx.Response(
		422,
		request=request,
		json={"errorCode": "HTTP_422", "httpStatus": 422},
	)
	error = httpx.HTTPStatusError("unprocessable", request=request, response=response)

	message = _describe_workday_error(error)

	assert message == "HTTP 422 (HTTP_422)"