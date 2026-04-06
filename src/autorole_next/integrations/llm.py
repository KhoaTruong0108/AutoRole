from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from autorole_next.config import LLMConfig

T = TypeVar("T", bound=BaseModel)


class LLMResponseError(Exception):
	"""Raised when an LLM call fails or returns invalid structured output."""


class LLMClient(ABC):
	@abstractmethod
	async def call(
		self,
		system: str,
		user: str,
		response_model: type[T] | None = None,
		temperature: float | None = None,
	) -> T | str:
		"""Call the configured LLM and return structured or raw output."""


class OpenAILLMClient(LLMClient):
	def __init__(self, config: LLMConfig) -> None:
		try:
			import openai
		except Exception as exc:  # pragma: no cover - depends on runtime environment
			raise RuntimeError("openai package is required for OpenAILLMClient") from exc

		self._client = openai.AsyncOpenAI(timeout=float(config.timeout_seconds))
		self._config = config

	async def call(
		self,
		system: str,
		user: str,
		response_model: type[T] | None = None,
		temperature: float | None = None,
	) -> T | str:
		temp = self._config.temperature if temperature is None else temperature
		messages = [
			{"role": "system", "content": system},
			{"role": "user", "content": user},
		]

		for attempt in range(self._config.max_retries):
			try:
				if response_model is not None:
					resp = await self._client.beta.chat.completions.parse(
						model=self._config.model,
						messages=messages,
						response_format=response_model,
						temperature=temp,
					)
					parsed = resp.choices[0].message.parsed
					if isinstance(parsed, response_model):
						return parsed
					return response_model.model_validate(parsed)

				resp = await self._client.chat.completions.create(
					model=self._config.model,
					messages=messages,
					temperature=temp,
				)
				return resp.choices[0].message.content or ""
			except Exception as exc:
				if attempt == self._config.max_retries - 1:
					raise LLMResponseError(
						f"OpenAI call failed after {self._config.max_retries} retries: {exc}"
					) from exc
				await asyncio.sleep(2**attempt)

		raise LLMResponseError("OpenAI call failed unexpectedly")


class AnthropicLLMClient(LLMClient):
	def __init__(self, config: LLMConfig) -> None:
		try:
			import anthropic
		except Exception as exc:  # pragma: no cover - depends on runtime environment
			raise RuntimeError("anthropic package is required for AnthropicLLMClient") from exc

		self._client = anthropic.AsyncAnthropic(timeout=float(config.timeout_seconds))
		self._config = config

	async def call(
		self,
		system: str,
		user: str,
		response_model: type[T] | None = None,
		temperature: float | None = None,
	) -> T | str:
		temp = self._config.temperature if temperature is None else temperature

		for attempt in range(self._config.max_retries):
			try:
				response = await self._client.messages.create(
					model=self._config.model,
					temperature=temp,
					system=system,
					max_tokens=4096,
					messages=[{"role": "user", "content": user}],
				)

				parts = [block.text for block in response.content if getattr(block, "type", "") == "text"]
				text = "\n".join(parts).strip()

				if response_model is None:
					return text

				# Anthropic text responses can include fences; strip common wrappers before parsing.
				clean = text.strip()
				if clean.startswith("```"):
					clean = clean.strip("`")
					if clean.startswith("json"):
						clean = clean[4:].strip()

				try:
					data = json.loads(clean)
				except Exception:
					data = clean

				return response_model.model_validate(data)
			except Exception as exc:
				if attempt == self._config.max_retries - 1:
					raise LLMResponseError(
						f"Anthropic call failed after {self._config.max_retries} retries: {exc}"
					) from exc
				await asyncio.sleep(2**attempt)

		raise LLMResponseError("Anthropic call failed unexpectedly")


class OllamaLLMClient(LLMClient):
	"""Local Ollama-backed LLM client using the /api/chat endpoint."""

	def __init__(self, config: LLMConfig) -> None:
		self._config = config

	async def call(
		self,
		system: str,
		user: str,
		response_model: type[T] | None = None,
		temperature: float | None = None,
	) -> T | str:
		temp = self._config.temperature if temperature is None else temperature
		url = f"{self._config.ollama_base_url.rstrip('/')}/api/chat"

		payload: dict[str, Any] = {
			"model": self._config.ollama_model or self._config.model,
			"messages": [
				{"role": "system", "content": system},
				{"role": "user", "content": user},
			],
			"stream": False,
			"options": {"temperature": temp},
		}
		if response_model is not None:
			payload["format"] = "json"

		for attempt in range(self._config.max_retries):
			try:
				async with httpx.AsyncClient(timeout=float(self._config.timeout_seconds)) as client:
					resp = await client.post(url, json=payload)
				resp.raise_for_status()
				data = resp.json()
				text = str(data.get("message", {}).get("content", ""))

				if response_model is None:
					return text

				clean = text.strip()
				if clean.startswith("```"):
					clean = clean.strip("`")
					if clean.startswith("json"):
						clean = clean[4:].strip()

				parsed = json.loads(clean)
				return response_model.model_validate(parsed)
			except Exception as exc:
				if attempt == self._config.max_retries - 1:
					raise LLMResponseError(
						f"Ollama call failed after {self._config.max_retries} retries: {exc}"
					) from exc
				await asyncio.sleep(2**attempt)

		raise LLMResponseError("Ollama call failed unexpectedly")

