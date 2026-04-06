from __future__ import annotations

from autorole_next.form_controls.models import DetectionMethod, DetectionResult

URL_PATTERNS: dict[str, str] = {
	"myworkdayjobs.com": "workday",
	"greenhouse.io": "greenhouse",
	"lever.co": "lever",
	"ashbyhq.com": "ashby",
}

DOM_FINGERPRINTS: dict[str, list[str]] = {
	"workday": ['[data-automation-id="formContainer"]'],
	"greenhouse": ["form#application_form"],
	"lever": ["#application-form", 'form[data-qa="application-form"]'],
	"ashby": ['[data-testid="application-form"]', "form[action*='ashby']"],
}


def _detect_from_url(url: str) -> str | None:
	lower_url = url.lower()
	for host, platform in URL_PATTERNS.items():
		if host in lower_url:
			return platform
	return None


async def _detect_from_dom(page: object) -> str | None:
	if not hasattr(page, "query_selector"):
		return None
	for platform, selectors in DOM_FINGERPRINTS.items():
		for selector in selectors:
			try:
				if await page.query_selector(selector):
					return platform
			except Exception:
				continue
	return None


async def _detect_from_iframes(page: object) -> tuple[str | None, object | None]:
	for frame in getattr(page, "frames", []):
		url = getattr(frame, "url", "") or ""
		platform = _detect_from_url(url)
		if platform:
			return platform, frame
	return None, None


async def detect(page: object, url: str, run_id: str) -> DetectionResult:
	platform = _detect_from_url(url)
	method: DetectionMethod = "url"

	if not platform:
		platform = await _detect_from_dom(page)
		method = "dom"

	used_iframe = False
	if not platform:
		platform, _frame = await _detect_from_iframes(page)
		if platform:
			method = "iframe"
			used_iframe = True

	if not platform:
		platform = "generic"
		method = "fallback"

	apply_url = getattr(page, "url", "") or url
	return DetectionResult(
		run_id=run_id,
		platform_id=platform,
		apply_url=apply_url,
		used_iframe=used_iframe,
		detection_method=method,
	)

