"""Runtime settings (environment only) and the numeric constants shared by every module."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Judge tolerance (Problem Statement 11.5). Internal checks use STRICT_TOLERANCE for margin.
TOLERANCE_KWH = 0.01
TOLERANCE_BDT = 0.01
STRICT_TOLERANCE = 1e-6
ROUND_DECIMALS = 4
HOURS_PER_DAY = 24

PROMPT_VERSION = "v1"

_BLANK_IS_NONE = ("openai_api_key", "openai_base_url", "openai_reasoning_effort", "gemini_api_key")


class Settings(BaseSettings):
    """All configuration comes from the environment / .env; nothing secret has a default."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: SecretStr | None = None
    openai_base_url: str | None = None
    openai_model: str = "gpt-5.4-mini"
    openai_fallback_model: str = "gpt-5.4-nano"
    openai_reasoning_effort: str | None = "none"

    gemini_api_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    gemini_model: str = "gemini-2.5-flash"

    llm_timeout_s: float = 6.0
    llm_total_budget_s: float = 12.0
    llm_max_concurrency: int = 48
    llm_max_output_tokens: int = 600
    crosscheck_enabled: bool = True

    cache_max_entries: int = 5000
    max_body_bytes: int = 262_144
    max_note_chars: int = 2000

    port: int = 8000
    log_level: str = "INFO"
    enable_optional_endpoints: bool = True

    @field_validator(*_BLANK_IS_NONE, mode="before")
    @classmethod
    def _blank_to_none(cls, value: Any) -> Any:
        """`OPENAI_API_KEY=` in a copied .env.example means 'not configured', not an empty key."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    def configured_providers(self) -> list[str]:
        """Provider names only - safe to log."""
        names: list[str] = []
        if self.openai_api_key is not None:
            names += [f"openai:{self.openai_model}", f"openai:{self.openai_fallback_model}"]
        if self.gemini_api_key is not None:
            names.append(f"gemini:{self.gemini_model}")
        return names


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
