from __future__ import annotations


class JobAutomationError(Exception):
	pass


class NavigationError(JobAutomationError):
	pass


class ExtractionError(JobAutomationError):
	pass


class MappingError(JobAutomationError):
	pass


class FillError(JobAutomationError):
	pass


class RequiredFieldFillError(JobAutomationError):
	def __init__(self, msg: str, failed_field_ids: list[str] | None = None) -> None:
		super().__init__(msg)
		self.failed_field_ids: list[str] = failed_field_ids or []


class SubmissionError(JobAutomationError):
	def __init__(self, msg: str, errors: list[str] | None = None):
		super().__init__(msg)
		self.page_errors: list[str] = errors or []
