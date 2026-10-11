"""LLM providers.

* ``offline`` (default): no LLM; the deterministic rule engine does everything.
* ``mock``: a deterministic fake LLM that exercises the full LLM code path
  without network access (used in tests and demos).
* ``openai``: any OpenAI-compatible ``/chat/completions`` endpoint: Groq,
  OpenRouter, Together, a local Ollama (``http://localhost:11434/v1``), OpenAI.

Every LLM failure degrades to the rule engine; the LLM never blocks a reply.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol

import httpx

from clusterx_os.config import Settings
from clusterx_os.integrations import IntegrationError, send

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class LLMClient(Protocol):
    name: str

    def complete_json(self, system: str, user: str) -> dict[str, Any] | None: ...

    def status(self) -> dict[str, Any]: ...


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the first JSON object out of a model response."""
    cleaned = _FENCE_RE.sub("", text.strip())
    candidates = [cleaned]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if 0 <= start < end:
        candidates.append(cleaned[start : end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


class OpenAICompatibleLLM:
    name = "openai"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 30.0,
        json_mode: bool = True,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.json_mode = json_mode
        self._client = client or httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def chat(self, messages: list[dict[str, str]]) -> str:
        body: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0.2}
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        resp = send("llm", self._client, "POST", f"{self.base_url}/chat/completions",
                    headers=self._headers(), json=body)
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise IntegrationError(f"llm returned an unexpected payload: {exc}") from exc
        if not isinstance(content, str):
            raise IntegrationError("llm returned non-text content")
        return content

    def complete_json(self, system: str, user: str) -> dict[str, Any] | None:
        try:
            content = self.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": user}]
            )
        except IntegrationError as exc:
            logger.warning("llm call failed; using rule engine", extra={"error": str(exc)})
            return None
        data = extract_json_object(content)
        if data is None:
            logger.warning("llm response was not JSON; using rule engine")
        return data

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "configured": True, "model": self.model,
                "base_url": self.base_url}


class MockLLM:
    """Deterministic stand-in: returns the rule-engine draft and echoes its intent."""

    name = "mock"

    def complete_json(self, system: str, user: str) -> dict[str, Any] | None:
        try:
            ctx = json.loads(user)
        except ValueError:
            return None
        draft = str(ctx.get("draft_reply", "")).strip()
        if not draft:
            return None
        rule = ctx.get("rule_intent") or {}
        return {
            "reply": draft,
            "intent": rule.get("label", "Cold"),
            "confidence": rule.get("confidence", 0.5),
            "reason": "mock llm agreed with rule engine",
            "slots": {},
        }

    def status(self) -> dict[str, Any]:
        return {"name": self.name, "configured": True, "model": "deterministic-mock"}


def get_llm(settings: Settings) -> LLMClient | None:
    if settings.LLM_PROVIDER == "mock":
        return MockLLM()
    if settings.LLM_PROVIDER == "openai":
        if not settings.LLM_BASE_URL:
            logger.error("LLM_PROVIDER=openai but LLM_BASE_URL is empty; running offline")
            return None
        return OpenAICompatibleLLM(
            settings.LLM_BASE_URL,
            settings.LLM_API_KEY,
            settings.LLM_MODEL,
            timeout=settings.LLM_TIMEOUT_SECONDS,
            json_mode=settings.LLM_JSON_MODE,
        )
    return None
