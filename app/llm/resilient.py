"""
Resilient LLM client  -- the ONE mandatory engineering improvement.

Retry & fallback logic, in four layers:
  1. Retry with exponential backoff + jitter on transient failures (honours Retry-After).
  2. Output self-repair: invalid JSON / schema mismatch is fed back to the model once per attempt.
  3. Provider fallback chain (e.g. Groq -> Gemini -> Ollama -> offline mock).
  4. Per-provider circuit breaker so a dead provider is skipped instead of retried for every task.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .base import (
    AllProvidersFailedError,
    LLMEvent,
    LLMOutputError,
    LLMProvider,
    PermanentLLMError,
    TransientLLMError,
)
from .jsonutil import extract_json

log = logging.getLogger("agent.llm")
T = TypeVar("T", bound=BaseModel)


@dataclass
class _Health:
    failures: int = 0
    open_until: float = 0.0


class ResilientLLM:
    def __init__(
        self,
        providers: list[LLMProvider],
        *,
        max_retries: int = 3,
        backoff_base_s: float = 0.8,
        circuit_threshold: int = 3,
        circuit_cooldown_s: float = 30.0,
    ) -> None:
        if not providers:
            raise ValueError("At least one LLM provider is required")
        self.providers = providers
        self.max_retries = max(1, max_retries)
        self.backoff_base_s = backoff_base_s
        self.circuit_threshold = circuit_threshold
        self.circuit_cooldown_s = circuit_cooldown_s
        self._health = {p.name: _Health() for p in providers}

    async def aclose(self) -> None:
        for p in self.providers:
            await p.aclose()

    def _delay(self, attempt: int, retry_after: float | None) -> float:
        backoff = self.backoff_base_s * (2 ** (attempt - 1)) * (0.5 + random.random())
        return min(max(backoff, retry_after or 0.0), 20.0)

    async def complete_json(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        model_cls: type[T],
        events: list[LLMEvent],
        temperature: float = 0.3,
    ) -> T:
        errors: list[str] = []
        for provider in self.providers:
            health = self._health[provider.name]
            if not provider.always_available and time.monotonic() < health.open_until:
                errors.append(f"{provider.name}: circuit open")
                continue

            messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
            started = time.monotonic()
            last_error = "unknown"
            attempts = 0

            for attempt in range(1, self.max_retries + 1):
                attempts = attempt
                raw: str | None = None
                try:
                    raw = await provider.chat(messages, purpose=purpose, json_mode=True, temperature=temperature)
                    result = model_cls.model_validate(extract_json(raw))
                except (LLMOutputError, ValidationError) as exc:
                    last_error = f"bad output: {str(exc)[:160]}"
                    log.warning("[%s/%s] attempt %d %s", provider.name, purpose, attempt, last_error)
                    if raw is not None:  # self-repair: show the model its own mistake
                        messages = messages + [
                            {"role": "assistant", "content": raw[:4000]},
                            {
                                "role": "user",
                                "content": f"Your reply was invalid ({str(exc)[:300]}). "
                                "Return ONLY a corrected JSON object that matches the requested schema.",
                            },
                        ]
                    continue
                except TransientLLMError as exc:
                    last_error = str(exc)
                    log.warning("[%s/%s] attempt %d transient: %s", provider.name, purpose, attempt, exc)
                    if attempt < self.max_retries:
                        await asyncio.sleep(self._delay(attempt, exc.retry_after))
                    continue
                except PermanentLLMError as exc:
                    last_error = str(exc)
                    log.warning("[%s/%s] permanent: %s", provider.name, purpose, exc)
                    break
                else:
                    health.failures = 0
                    events.append(
                        LLMEvent(
                            purpose=purpose,
                            provider=provider.name,
                            attempts=attempts,
                            ok=True,
                            latency_ms=int((time.monotonic() - started) * 1000),
                        )
                    )
                    return result

            # provider exhausted -> record, trip breaker, fall through to next provider
            health.failures += 1
            if health.failures >= self.circuit_threshold:
                health.open_until = time.monotonic() + self.circuit_cooldown_s
            events.append(
                LLMEvent(
                    purpose=purpose,
                    provider=provider.name,
                    attempts=attempts,
                    ok=False,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    error=last_error,
                )
            )
            errors.append(f"{provider.name}: {last_error}")

        raise AllProvidersFailedError("; ".join(errors))
