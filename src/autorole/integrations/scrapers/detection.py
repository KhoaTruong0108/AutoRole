from __future__ import annotations

ATS_PATTERNS: dict[str, list[str]] = {
	"lever": ["jobs.lever.co", "lever.co/"],
	"greenhouse": ["boards.greenhouse.io", "grnh.se", "greenhouse.io"],
	"linkedin": ["linkedin.com/jobs"],
	"indeed": ["indeed.com/viewjob", "indeed.com/rc/clk"],
	"workday": ["myworkdayjobs.com", "workday.com/en-US/jobs"],
	"ashby": ["ashbyhq.com", "jobs.ashby.com"],
	"bamboohr": ["bamboohr.com/jobs"],
	"icims": ["icims.com"],
	"smartrecruiters": ["smartrecruiters.com"],
}


def detect_ats(url: str) -> str:
	"""Return ATS platform name from URL, defaulting to generic."""
	lower = url.lower()
	for ats, patterns in ATS_PATTERNS.items():
		if any(pattern in lower for pattern in patterns):
			return ats
	return "generic"
