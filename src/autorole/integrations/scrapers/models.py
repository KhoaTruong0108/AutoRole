from __future__ import annotations

from pydantic import BaseModel


class JobMetadata(BaseModel):
	"""Search-phase metadata for one discovered listing."""

	model_config = {"frozen": True}

	job_id: str
	job_title: str
	company_name: str
	location: str = ""
	employment_type: str = ""
	job_url: str
	apply_url: str = ""
	posted_at: str = ""
	department: str = ""
	team: str = ""


class JobDescription(BaseModel):
	"""JD-phase structured extraction output."""

	model_config = {"frozen": True}

	job_id: str
	job_title: str
	company_name: str
	location: str = ""
	employment_type: str = ""
	raw_html: str
	plain_text: str
	qualifications: list[str]
	responsibilities: list[str]
	preferred_skills: list[str]
	culture_signals: list[str]


class FormField(BaseModel):
	"""One application form field."""

	model_config = {"frozen": True}

	name: str
	label: str
	field_type: str
	required: bool
	options: list[str]
	placeholder: str = ""
	map_key: str = ""


class ApplicationForm(BaseModel):
	"""Form-phase extraction output."""

	model_config = {"frozen": True}

	job_id: str
	apply_url: str
	fields: list[FormField]
	submit_selector: str
	form_selector: str
