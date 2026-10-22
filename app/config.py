"""Central configuration. Every knob is an environment variable (12-factor)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Provider chain (comma separated, tried in order)
    llm_providers: str = "groq,gemini,ollama,mock"

    groq_api_key: str | None = None
    groq_model: str = "llama-3.3-70b-versatile"
    groq_base_url: str = "https://api.groq.com/openai/v1"

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.0-flash"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"

    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "llama3.1:8b"

    # Resilience
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 3
    llm_backoff_base_s: float = 0.8
    llm_circuit_threshold: int = 3
    llm_circuit_cooldown_s: float = 30.0

    # Agent
    max_concurrency: int = 3  # parallel LLM calls (free tiers have tight rate limits)
    max_tasks: int = 12
    output_dir: Path = Path("outputs")
    knowledge_base_path: Path = Path(__file__).parent / "data" / "knowledge_base.json"


@lru_cache
def get_settings() -> Settings:
    return Settings()
