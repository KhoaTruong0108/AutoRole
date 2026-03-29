from __future__ import annotations

import pytest

from autorole.config import SearchFilter
from autorole.integrations.discovery import build_discovery_providers
from autorole.integrations.discovery import smartextract as smartextract_module
from autorole.integrations.discovery.smartextract import (
	DirectSite,
	SmartExtractDiscoveryProvider,
	_build_targets,
	_clean_html_for_llm,
	_extract_job_postings_from_html,
	_fallback_parse_sites_yaml,
	load_sites,
	_posting_to_listing,
)


def test_build_discovery_providers_includes_smartextract() -> None:
	providers = build_discovery_providers(["smartextract", "workday"])

	assert "smartextract" in providers
	assert isinstance(providers["smartextract"], SmartExtractDiscoveryProvider)


def test_load_sites_returns_packaged_registry() -> None:
	sites = load_sites()

	assert len(sites) >= 3
	assert any(site.name == "RemoteOK" for site in sites)


def test_fallback_parse_sites_yaml_ignores_non_site_sections() -> None:
	text = """
	manual_ats:
	  - "ibegin.tcsapps.com"
	blocked:
	  sites:
	    - "glassdoor"
	base_urls:
	  "RemoteOK": null
	sites:
	  - name: "RemoteOK"
	    url: "https://remoteok.com/remote-dev-jobs"
	    type: static
	  - name: "PowerToFly"
	    url: "https://powertofly.com/jobs/?keywords={query_encoded}"
	    type: search
	"""

	parsed = _fallback_parse_sites_yaml(text)

	assert parsed == {
		"sites": [
			{
				"name": "RemoteOK",
				"url": "https://remoteok.com/remote-dev-jobs",
				"type": "static",
			},
			{
				"name": "PowerToFly",
				"url": "https://powertofly.com/jobs/?keywords={query_encoded}",
				"type": "search",
			},
		]
	}


class _FakeResponse:
	def __init__(self, text: str) -> None:
		self.text = text

	def raise_for_status(self) -> None:
		return None


class _FakeAsyncClient:
	def __init__(self, *_args, **_kwargs) -> None:
		pass

	async def __aenter__(self) -> "_FakeAsyncClient":
		return self

	async def __aexit__(self, exc_type, exc, tb) -> None:
		_ = (exc_type, exc, tb)
		return None

	async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
		_ = headers
		html = f"""
		<html><body>
		<script type=\"application/ld+json\">
		{{
		  \"@context\": \"https://schema.org\",
		  \"@type\": \"JobPosting\",
		  \"title\": \"Platform Engineer\",
		  \"url\": \"https://boards.greenhouse.io/acme/jobs/123456\",
		  \"hiringOrganization\": {{\"name\": \"Acme\"}},
		  \"identifier\": {{\"value\": \"gh-123456\"}},
		  \"jobLocation\": {{\"address\": {{\"addressLocality\": \"Remote\", \"addressCountry\": \"US\"}}}}
		}}
		</script>
		</body></html>
		"""
		if "dupe" in url:
			return _FakeResponse(html)
		return _FakeResponse(html)


class _SslFailingAsyncClient(_FakeAsyncClient):
	async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
		_ = headers
		if "bad.example.com" in url:
			raise httpx.ConnectError("[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] ssl/tls alert handshake failure")
		return await super().get(url, headers=headers)


class _FakeLLMClient:
	async def call(self, system: str, user: str, response_model=None, temperature: float | None = None):
		_ = (system, user, temperature)
		assert response_model is not None
		return response_model.model_validate(
			{
				"jobs": [
					{
						"title": "ML Platform Engineer",
						"company_name": "Acme",
						"job_url": "https://jobs.ashbyhq.com/acme/12345",
						"location": "Remote",
					}
				]
			}
		)


class _SelectorFallbackLLMClient:
	async def call(self, system: str, user: str, response_model=None, temperature: float | None = None):
		_ = (system, user, temperature)
		assert response_model is not None
		fields = getattr(response_model, "model_fields", {})
		if "jobs" in fields:
			return response_model.model_validate({"jobs": []})
		return response_model.model_validate(
			{
				"job_card": "article.job-card",
				"title": "h2 a",
				"url": "h2 a",
				"location": ".location",
			}
		)


class _ButtonCardSelectorLLMClient:
	async def call(self, system: str, user: str, response_model=None, temperature: float | None = None):
		_ = (system, user, temperature)
		assert response_model is not None
		fields = getattr(response_model, "model_fields", {})
		if "jobs" in fields:
			return response_model.model_validate({"jobs": []})
		return response_model.model_validate(
			{
				"job_card": "button.job-card",
				"title": "h2",
				"location": ".location",
			}
		)


@pytest.mark.asyncio
async def test_smartextract_provider_search_normalizes_json_ld(monkeypatch) -> None:
	monkeypatch.setattr(smartextract_module.httpx, "AsyncClient", _FakeAsyncClient)
	provider = SmartExtractDiscoveryProvider(
		sites=[
			DirectSite(name="Site A", url="https://example.com/jobs?q={query_encoded}", type="search"),
			DirectSite(name="Site B", url="https://example.com/dupe?q={query_encoded}", type="search"),
		]
	)

	results = await provider.search(
		SearchFilter(platforms=["smartextract"], keywords=["platform engineer"], location="Remote")
	)

	assert len(results) == 1
	listing = results[0]
	assert listing.company_name == "Acme"
	assert listing.job_title == "Platform Engineer"
	assert listing.job_id == "gh-123456"
	assert listing.platform == "greenhouse"


@pytest.mark.asyncio
async def test_smartextract_provider_continues_when_one_site_fails_ssl(monkeypatch) -> None:
	monkeypatch.setattr(smartextract_module.httpx, "AsyncClient", _SslFailingAsyncClient)
	provider = SmartExtractDiscoveryProvider(
		sites=[
			DirectSite(name="Bad Site", url="https://bad.example.com/jobs?q={query_encoded}", type="search"),
			DirectSite(name="Good Site", url="https://good.example.com/jobs?q={query_encoded}", type="search"),
		]
	)

	results = await provider.search(
		SearchFilter(platforms=["smartextract"], keywords=["platform engineer"], location="Remote")
	)

	assert len(results) == 1
	listing = results[0]
	assert listing.company_name == "Acme"
	assert listing.job_title == "Platform Engineer"
	assert listing.platform == "greenhouse"


@pytest.mark.asyncio
async def test_smartextract_provider_search_uses_llm_without_json_ld(monkeypatch) -> None:
	class _NoJsonLdClient(_FakeAsyncClient):
		async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
			_ = (url, headers)
			return _FakeResponse(
				"<html><body><main><a href='https://jobs.ashbyhq.com/acme/12345'>ML Platform Engineer</a></main></body></html>"
			)

	monkeypatch.setattr(smartextract_module.httpx, "AsyncClient", _NoJsonLdClient)
	provider = SmartExtractDiscoveryProvider(
		sites=[DirectSite(name="Site A", url="https://example.com/jobs?q={query_encoded}", type="search")],
		llm_client=_FakeLLMClient(),
	)

	results = await provider.search(
		SearchFilter(platforms=["smartextract"], keywords=["ml platform"], location="Remote")
	)

	assert len(results) == 1
	listing = results[0]
	assert listing.company_name == "Acme"
	assert listing.job_title == "ML Platform Engineer"
	assert listing.platform == "ashby"


def test_clean_html_for_llm_removes_script_noise() -> None:
	html = "<html><body><script>alert(1)</script><main><div>Role</div></main></body></html>"

	cleaned = _clean_html_for_llm(html)

	assert "alert(1)" not in cleaned
	assert "Role" in cleaned


@pytest.mark.asyncio
async def test_smartextract_provider_search_uses_rendered_html_when_http_has_no_json_ld(monkeypatch) -> None:
	class _NoJsonLdClient(_FakeAsyncClient):
		async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
			_ = (url, headers)
			return _FakeResponse("<html><body><main>No structured jobs</main></body></html>")

	async def fake_render_html(url: str) -> str:
		_ = url
		return """
		<html><body>
		<script type=\"application/ld+json\">
		{
		  \"@context\": \"https://schema.org\",
		  \"@type\": \"JobPosting\",
		  \"title\": \"Rendered Platform Engineer\",
		  \"url\": \"https://boards.greenhouse.io/acme/jobs/999999\",
		  \"hiringOrganization\": {\"name\": \"Acme\"},
		  \"identifier\": {\"value\": \"gh-999999\"},
		  \"jobLocation\": {\"address\": {\"addressLocality\": \"Remote\", \"addressCountry\": \"US\"}}
		}
		</script>
		</body></html>
		"""

	monkeypatch.setattr(smartextract_module.httpx, "AsyncClient", _NoJsonLdClient)
	provider = SmartExtractDiscoveryProvider(
		sites=[DirectSite(name="Site A", url="https://example.com/jobs?q={query_encoded}", type="search")],
		render_html=fake_render_html,
	)

	results = await provider.search(
		SearchFilter(platforms=["smartextract"], keywords=["platform engineer"], location="Remote")
	)

	assert len(results) == 1
	listing = results[0]
	assert listing.job_title == "Rendered Platform Engineer"
	assert listing.job_id == "gh-999999"
	assert listing.platform == "greenhouse"


@pytest.mark.asyncio
async def test_smartextract_provider_search_uses_selector_fallback_after_empty_llm_extract(monkeypatch) -> None:
	class _CardOnlyClient(_FakeAsyncClient):
		async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
			_ = (url, headers)
			return _FakeResponse(
				"""
				<html><body><main>
				<article class='job-card'>
				  <h2><a href='/jobs/123'>Platform Engineer</a></h2>
				  <div class='location'>Remote</div>
				</article>
				<article class='job-card'>
				  <h2><a href='/jobs/123'>Platform Engineer</a></h2>
				  <div class='location'>Remote</div>
				</article>
				</main></body></html>
				"""
			)

	monkeypatch.setattr(smartextract_module.httpx, "AsyncClient", _CardOnlyClient)
	provider = SmartExtractDiscoveryProvider(
		sites=[DirectSite(name="Site A", url="https://example.com/jobs?q={query_encoded}", type="search")],
		llm_client=_SelectorFallbackLLMClient(),
	)

	results = await provider.search(
		SearchFilter(platforms=["smartextract"], keywords=["platform engineer"], location="Remote")
	)

	assert len(results) == 1
	listing = results[0]
	assert listing.job_title == "Platform Engineer"
	assert listing.job_url == "https://example.com/jobs/123"
	assert listing.apply_url == "https://example.com/jobs/123"
	assert listing.platform == "smartextract"


@pytest.mark.asyncio
async def test_smartextract_selector_fallback_uses_button_action_when_url_selector_missing(monkeypatch) -> None:
	class _ButtonCardClient(_FakeAsyncClient):
		async def get(self, url: str, headers: dict[str, str] | None = None) -> _FakeResponse:
			_ = (url, headers)
			return _FakeResponse(
				"""
				<html><body><main>
				<button class='job-card' action='/jobs/detail/123'>
				  <h2>Platform Engineer</h2>
				  <span class='location'>Remote</span>
				</button>
				<button class='job-card' action='/jobs/detail/123'>
				  <h2>Platform Engineer</h2>
				  <span class='location'>Remote</span>
				</button>
				</main></body></html>
				"""
			)

	monkeypatch.setattr(smartextract_module.httpx, "AsyncClient", _ButtonCardClient)
	provider = SmartExtractDiscoveryProvider(
		sites=[DirectSite(name="Site A", url="https://example.com/jobs?q={query_encoded}", type="search")],
		llm_client=_ButtonCardSelectorLLMClient(),
	)

	results = await provider.search(
		SearchFilter(platforms=["smartextract"], keywords=["platform engineer"], location="")
	)

	assert len(results) == 1
	listing = results[0]
	assert listing.job_title == "Platform Engineer"
	assert listing.job_url == "https://example.com/jobs/detail/123"
	assert listing.apply_url == "https://example.com/jobs/detail/123"


def test_build_targets_expands_search_and_static_sites() -> None:
	sites = (
		DirectSite(name="Search Site", url="https://example.com/jobs?q={query_encoded}&l={location_encoded}", type="search"),
		DirectSite(name="Static Site", url="https://example.com/static", type="static"),
	)

	targets = _build_targets(sites, SearchFilter(platforms=["smartextract"], keywords=["platform engineer"], location="Remote"))

	assert targets == [
		("Search Site", "https://example.com/jobs?q=platform+engineer&l=Remote"),
		("Static Site", "https://example.com/static"),
	]


def test_extract_job_postings_from_html_reads_json_ld() -> None:
	html = """
	<html><body>
	<script type="application/ld+json">
	{
	  "@context": "https://schema.org",
	  "@type": "JobPosting",
	  "title": "Platform Engineer",
	  "url": "https://jobs.example.com/123",
	  "hiringOrganization": {"name": "Acme"},
	  "identifier": {"value": "job-123"}
	}
	</script>
	</body></html>
	"""

	postings = _extract_job_postings_from_html(html)

	assert len(postings) == 1
	assert postings[0]["title"] == "Platform Engineer"


def test_extract_job_postings_from_html_uses_parent_row_url_when_json_ld_omits_it() -> None:
	html = """
	<html><body>
	<tr class="job" data-url="/remote-jobs/remote-platform-engineer-acme-123" data-id="123">
	  <script type="application/ld+json">
	  {
	    "@context": "https://schema.org",
	    "@type": "JobPosting",
	    "title": "Platform Engineer",
	    "hiringOrganization": {"name": "Acme"}
	  }
	  </script>
	</tr>
	</body></html>
	"""

	postings = _extract_job_postings_from_html(html)

	assert len(postings) == 1
	assert postings[0]["url"] == "/remote-jobs/remote-platform-engineer-acme-123"
	assert postings[0]["identifier"] == {"value": "123"}


def test_posting_to_listing_detects_ats_from_url() -> None:
	posting = {
		"title": "Backend Engineer",
		"url": "https://boards.greenhouse.io/acme/jobs/123456",
		"hiringOrganization": {"name": "Acme"},
		"identifier": {"value": "gh-123456"},
	}

	listing = _posting_to_listing("Acme Careers", posting, "https://example.com")

	assert listing is not None
	assert listing.company_name == "Acme"
	assert listing.job_id == "gh-123456"
	assert listing.platform == "greenhouse"
	assert listing.apply_url == "https://boards.greenhouse.io/acme/jobs/123456"


def test_posting_to_listing_normalizes_relative_url_against_source() -> None:
	posting = {
		"title": "Backend Engineer",
		"url": "/remote-jobs/remote-backend-engineer-acme-123",
		"hiringOrganization": {"name": "Acme"},
	}

	listing = _posting_to_listing("Acme Careers", posting, "https://remoteok.com/remote-dev-jobs")

	assert listing is not None
	assert listing.job_url == "https://remoteok.com/remote-jobs/remote-backend-engineer-acme-123"
	assert listing.apply_url == "https://remoteok.com/remote-jobs/remote-backend-engineer-acme-123"