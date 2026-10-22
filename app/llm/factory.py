from __future__ import annotations

import logging

from app.config import Settings

from .base import LLMProvider
from .mock import MockProvider
from .openai_compat import OpenAICompatProvider
from .resilient import ResilientLLM

log = logging.getLogger("agent.llm")


def build_providers(s: Settings) -> list[LLMProvider]:
    providers: list[LLMProvider] = []
    for name in [n.strip().lower() for n in s.llm_providers.split(",") if n.strip()]:
        if name == "groq":
            if s.groq_api_key:
                providers.append(OpenAICompatProvider("groq", s.groq_base_url, s.groq_model, s.groq_api_key, s.llm_timeout_s))
            else:
                log.info("groq skipped: GROQ_API_KEY not set")
        elif name == "gemini":
            if s.gemini_api_key:
                providers.append(OpenAICompatProvider("gemini", s.gemini_base_url, s.gemini_model, s.gemini_api_key, s.llm_timeout_s))
            else:
                log.info("gemini skipped: GEMINI_API_KEY not set")
        elif name == "ollama":
            providers.append(OpenAICompatProvider("ollama", s.ollama_base_url, s.ollama_model, None, s.llm_timeout_s))
        elif name == "mock":
            providers.append(MockProvider())
        else:
            log.warning("unknown provider %r ignored", name)
    return providers


def build_llm(s: Settings) -> ResilientLLM:
    return ResilientLLM(
        build_providers(s),
        max_retries=s.llm_max_retries,
        backoff_base_s=s.llm_backoff_base_s,
        circuit_threshold=s.llm_circuit_threshold,
        circuit_cooldown_s=s.llm_circuit_cooldown_s,
    )
