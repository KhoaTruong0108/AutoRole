from __future__ import annotations

from bs4 import BeautifulSoup

from autorole.integrations.scrapers.greenhouse import (
	_extract_bullets_by_heading as gh_extract_bullets,
	_map_greenhouse_type,
	_parse_greenhouse_url,
)
from autorole.integrations.scrapers.lever import _extract_bullets as lever_extract_bullets
from autorole.integrations.scrapers.lever import _parse_lever_url


def test_parse_lever_url_job_page() -> None:
	company, posting_id = _parse_lever_url("https://jobs.lever.co/aircall/43905627-fa43")
	assert company == "aircall"
	assert posting_id == "43905627-fa43"


def test_parse_lever_url_apply_page() -> None:
	company, posting_id = _parse_lever_url("https://jobs.lever.co/aircall/43905627-fa43/apply")
	assert company == "aircall"
	assert posting_id == "43905627-fa43"


def test_parse_greenhouse_url() -> None:
	board, job_id = _parse_greenhouse_url("https://boards.greenhouse.io/webflow/jobs/4553486004")
	assert board == "webflow"
	assert job_id == "4553486004"


def test_lever_extract_bullets_matches_section() -> None:
	posting = {
		"lists": [
			{"text": "What you'll do", "content": "<li>Build APIs</li><li>Improve DX</li>"},
			{"text": "Benefits", "content": "<li>Health</li>"},
		]
	}
	result = lever_extract_bullets(posting, ["you'll do", "responsibilities"])
	assert result == ["Build APIs", "Improve DX"]


def test_greenhouse_extract_bullets_by_heading() -> None:
	html = """
	<div>
	  <h3>Qualifications</h3>
	  <ul><li>Python</li><li>Distributed systems</li></ul>
	</div>
	"""
	soup = BeautifulSoup(html, "html.parser")
	result = gh_extract_bullets(soup, ["qualifications", "requirements"])
	assert result == ["Python", "Distributed systems"]


def test_greenhouse_type_mapping() -> None:
	assert _map_greenhouse_type("input_text") == "text"
	assert _map_greenhouse_type("input_file") == "file"
	assert _map_greenhouse_type("multi_value_single_select") == "select"
	assert _map_greenhouse_type("unknown") == "text"
