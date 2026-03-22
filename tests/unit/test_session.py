from __future__ import annotations

from typing import Any

from autorole.context import JobApplicationContext
from autorole.stages import session as session_mod
from autorole.stages.session import SessionStage
from tests.conftest import SAMPLE_LISTING

try:
	from pipeline.types import Message
except Exception:
	class Message:  # pragma: no cover - fallback when pipeline package is unavailable
		def __init__(self, run_id: str, payload: dict[str, Any], metadata: dict[str, Any] | None = None) -> None:
			self.run_id = run_id
			self.payload = payload
			self.metadata = metadata or {}


class FakeCredentialStore:
	def __init__(self, values: dict[str, str] | None = None) -> None:
		self.values = values or {}

	def get(self, key: str) -> str | None:
		return self.values.get(key)


async def test_session_passes_for_public_platform(test_config: Any) -> None:
	listing = SAMPLE_LISTING.model_copy(update={"platform": "greenhouse"})
	ctx = JobApplicationContext(run_id="r1", listing=listing)
	stage = SessionStage(test_config, FakeCredentialStore())

	result = await stage.execute(Message(run_id="r1", payload=ctx.model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.session is not None
	assert out_ctx.session.authenticated is False


async def test_session_passes_with_valid_stored_cookie(test_config: Any, monkeypatch: Any) -> None:
	listing = SAMPLE_LISTING.model_copy(update={"platform": "linkedin"})
	ctx = JobApplicationContext(run_id="r1", listing=listing)

	async def fake_validate(_platform: str, _cookie: str) -> bool:
		return True

	monkeypatch.setattr(session_mod, "_validate_session", fake_validate)
	stage = SessionStage(test_config, FakeCredentialStore({"linkedin_cookie": "cookie-val"}))

	result = await stage.execute(Message(run_id="r1", payload=ctx.model_dump()))

	assert result.success
	out_ctx = JobApplicationContext.model_validate(result.output)
	assert out_ctx.session is not None
	assert out_ctx.session.authenticated is True


async def test_session_fails_with_expired_cookie(test_config: Any, monkeypatch: Any) -> None:
	listing = SAMPLE_LISTING.model_copy(update={"platform": "linkedin"})
	ctx = JobApplicationContext(run_id="r1", listing=listing)

	async def fake_validate(_platform: str, _cookie: str) -> bool:
		return False

	monkeypatch.setattr(session_mod, "_validate_session", fake_validate)
	stage = SessionStage(test_config, FakeCredentialStore({"linkedin_cookie": "cookie-val"}))

	result = await stage.execute(Message(run_id="r1", payload=ctx.model_dump()))

	assert not result.success
	assert result.error_type == "ExpiredSession"


async def test_session_fails_with_missing_credentials(test_config: Any) -> None:
	listing = SAMPLE_LISTING.model_copy(update={"platform": "linkedin"})
	ctx = JobApplicationContext(run_id="r1", listing=listing)
	stage = SessionStage(test_config, FakeCredentialStore())

	result = await stage.execute(Message(run_id="r1", payload=ctx.model_dump()))

	assert not result.success
	assert result.error_type == "MissingCredentials"
	assert "ar credentials set linkedin_cookie" in (result.error or "")


async def test_session_fails_when_listing_is_none(test_config: Any) -> None:
	ctx = JobApplicationContext(run_id="r1")
	stage = SessionStage(test_config, FakeCredentialStore())

	result = await stage.execute(Message(run_id="r1", payload=ctx.model_dump()))

	assert not result.success
	assert result.error_type == "PreconditionError"
