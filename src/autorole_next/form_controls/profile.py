from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UserProfile(BaseModel):
	"""Flexible profile model that accepts additional keys from user JSON."""

	model_config = ConfigDict(extra="allow")

	personal: dict[str, Any] = Field(default_factory=dict)
	work_authorization: dict[str, Any] = Field(default_factory=dict)
	employment: dict[str, Any] = Field(default_factory=dict)
	education: list[dict[str, Any]] = Field(default_factory=list)
	narrative: dict[str, Any] = Field(default_factory=dict)
	resume_path: str = ""


def load_profile(path: Path) -> UserProfile:
	raw = json.loads(path.read_text(encoding="utf-8"))
	return UserProfile.model_validate(raw)
