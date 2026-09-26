"""Pluggable AI layer.

LocalProvider (default): zero extra deps beyond numpy. Deterministic hashed
embeddings, extractive summarization, keyword-overlap classification.
Every product feature MUST work fully in local mode.

OpenAICompatibleProvider: used only when LLM_BASE_URL and LLM_API_KEY are set.
It enhances (not replaces) local results.
"""

from __future__ import annotations

import hashlib
import re
from typing import Protocol

import numpy as np

EMBED_DIM = 256


class AIProvider(Protocol):
    def chat(self, messages: list[dict]) -> str: ...
    def embed(self, texts: list[str]) -> list[list[float]]: ...
    def summarize(self, text: str, max_sentences: int = 3) -> str: ...


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


class LocalProvider:
    name = "local"

    def embed(self, texts: list[str]) -> list[list[float]]:
        vecs: list[list[float]] = []
        for text in texts:
            v = np.zeros(EMBED_DIM, dtype=np.float64)
            for tok in _tokens(text):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
                v[h % EMBED_DIM] += 1.0
            norm = float(np.linalg.norm(v))
            vecs.append((v / norm).tolist() if norm > 0 else v.tolist())
        return vecs

    def summarize(self, text: str, max_sentences: int = 3) -> str:
        sents = _sentences(text)
        if len(sents) <= max_sentences:
            return " ".join(sents)
        freq: dict[str, int] = {}
        for tok in _tokens(text):
            freq[tok] = freq.get(tok, 0) + 1
        scored = sorted(
            range(len(sents)),
            key=lambda i: (-sum(freq.get(t, 0) for t in set(_tokens(sents[i]))), i),
        )
        keep = sorted(scored[:max_sentences])
        return " ".join(sents[i] for i in keep)

    def chat(self, messages: list[dict]) -> str:
        context = "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")
        user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), ""
        )
        base = (context + "\n" + user).strip()
        if not base:
            return "No input provided."
        return self.summarize(base, 3)

    def classify(self, text: str, label_keywords: dict[str, list[str]]) -> str:
        toks = set(_tokens(text))
        best, best_score = "unknown", 0
        for label, keywords in label_keywords.items():
            score = sum(1 for kw in keywords if kw.lower() in toks)
            if score > best_score:
                best, best_score = label, score
        return best


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(
        self, base_url: str, api_key: str, chat_model: str, embed_model: str
    ) -> None:
        import httpx

        self._http = httpx
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.chat_model = chat_model
        self.embed_model = embed_model
        self._local = LocalProvider()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def chat(self, messages: list[dict]) -> str:
        try:
            r = self._http.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json={"model": self.chat_model, "messages": messages, "temperature": 0.2},
                timeout=60,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            return self._local.chat(messages)

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            r = self._http.post(
                f"{self.base_url}/embeddings",
                headers=self._headers(),
                json={"model": self.embed_model, "input": texts},
                timeout=60,
            )
            r.raise_for_status()
            return [d["embedding"] for d in r.json()["data"]]
        except Exception:
            return self._local.embed(texts)

    def summarize(self, text: str, max_sentences: int = 3) -> str:
        out = self.chat(
            [
                {"role": "system", "content": f"Summarize in at most {max_sentences} sentences."},
                {"role": "user", "content": text},
            ]
        )
        return out or self._local.summarize(text, max_sentences)


def get_provider() -> AIProvider:
    from app.core.config import settings

    if settings.LLM_BASE_URL and settings.LLM_API_KEY:
        return OpenAICompatibleProvider(
            settings.LLM_BASE_URL,
            settings.LLM_API_KEY,
            settings.LLM_CHAT_MODEL,
            settings.LLM_EMBED_MODEL,
        )
    return LocalProvider()


def cosine(a: list[float] | np.ndarray, b: list[float] | np.ndarray) -> float:
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)
