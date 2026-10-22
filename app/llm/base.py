"""LLM abstractions: provider protocol, error taxonomy and call telemetry."""
from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel


class LLMError(Exception):
    """Base class for all LLM-layer errors."""


class TransientLLMError(LLMError):
    """Worth retrying on the same provider (429, 5xx, timeouts)."""

    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class PermanentLLMError(LLMError):
    """Retrying the same provider is pointless (bad key, unreachable host, 4xx)."""


class LLMOutputError(LLMError):
    """The model answered, but not with valid JSON / the expected schema."""


class AllProvidersFailedError(LLMError):
    """Every provider in the chain was exhausted."""


class LLMEvent(BaseModel):
    """One provider attempt-sequence for one logical LLM call (for observability)."""

    purpose: str
    provider: str
    attempts: int
    ok: bool
    latency_ms: int
    error: str | None = None


class LLMProvider(Protocol):
    name: str
    always_available: bool  # True for the offline last-resort provider

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        purpose: str,
        json_mode: bool = True,
        temperature: float = 0.3,
    ) -> str: ...

    async def aclose(self) -> None: ...
