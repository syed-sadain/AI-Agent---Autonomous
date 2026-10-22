"""One provider class for every OpenAI-compatible endpoint (Groq, Gemini, Ollama, LM Studio...)."""
from __future__ import annotations

import httpx

from .base import PermanentLLMError, TransientLLMError


class OpenAICompatProvider:
    always_available = False

    def __init__(
        self,
        name: str,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self._timeout = timeout_s
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def chat(self, messages, *, purpose, json_mode=True, temperature=0.3) -> str:
        payload: dict = {"model": self.model, "messages": messages, "temperature": temperature}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            resp = await self.client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
        except httpx.ConnectError as exc:
            # Host is down / refused: retrying the same provider just burns time.
            raise PermanentLLMError(f"{self.name} unreachable: {exc}") from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientLLMError(f"{self.name} network error: {exc!r}") from exc

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = None
            try:
                retry_after = float(resp.headers.get("retry-after", ""))
            except ValueError:
                pass
            raise TransientLLMError(f"{self.name} HTTP {resp.status_code}", retry_after=retry_after)
        if resp.status_code >= 400:
            raise PermanentLLMError(f"{self.name} HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            return resp.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError) as exc:
            raise TransientLLMError(f"{self.name} malformed response envelope") from exc

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
