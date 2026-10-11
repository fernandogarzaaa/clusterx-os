"""Pluggable adapters for external systems (LLM, CRM, calendar, messaging)."""

from __future__ import annotations

from typing import Any

import httpx


class IntegrationError(RuntimeError):
    """An external call failed. ``retryable`` says whether a retry may succeed."""

    def __init__(
        self, message: str, *, retryable: bool = False, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class ConfigurationError(RuntimeError):
    """An adapter is selected but lacks the credentials or settings it needs."""


def raise_for_response(provider: str, resp: httpx.Response) -> None:
    """Translate an HTTP error response into an IntegrationError."""
    if resp.status_code < 400:
        return
    raise IntegrationError(
        f"{provider} API error {resp.status_code}: {resp.text[:300]}",
        retryable=resp.status_code == 429 or resp.status_code >= 500,
        status_code=resp.status_code,
    )


def send(
    provider: str, client: httpx.Client, method: str, url: str, **kwargs: Any
) -> httpx.Response:
    """Perform a request; network failures become retryable IntegrationErrors."""
    try:
        resp = client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        raise IntegrationError(f"{provider} request timed out: {exc}", retryable=True) from exc
    except httpx.HTTPError as exc:
        raise IntegrationError(f"{provider} request failed: {exc}", retryable=True) from exc
    raise_for_response(provider, resp)
    return resp
