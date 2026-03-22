from __future__ import annotations

import os

import keyring

SERVICE_NAME = "auto_role"


class CredentialStore:
	def get(self, key: str) -> str | None:
		try:
			value = keyring.get_password(SERVICE_NAME, key)
		except Exception:
			value = None
		if value:
			return value
		env_key = f"AR_{key.upper()}"
		return os.environ.get(env_key)

	def set(self, key: str, value: str) -> None:
		keyring.set_password(SERVICE_NAME, key, value)

	def delete(self, key: str) -> None:
		try:
			keyring.delete_password(SERVICE_NAME, key)
		except keyring.errors.PasswordDeleteError:
			pass
		except Exception:
			pass
