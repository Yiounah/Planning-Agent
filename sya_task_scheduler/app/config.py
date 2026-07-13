"""Global configuration for the cognitive-aware scheduling backend."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized runtime configuration with strict validation semantics.

    The scheduler relies on threshold-driven replanning policies, so the
    low/high completion thresholds are validated to avoid contradictory values.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SYA_",
        extra="ignore",
        validate_default=True,
    )

    app_name: str = "SYA Task Scheduler"
    debug: bool = False

    completion_low_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    completion_high_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    evaluation_leaf_batch_size: int = Field(default=3, ge=1)
    metrics_window_size: int = Field(default=10, ge=1)

    event_history_maxlen: int = Field(default=2000, ge=100)
    ws_ping_interval_seconds: int = Field(default=20, ge=5)

    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    llm_timeout_seconds: int = Field(default=45, ge=5)

    @model_validator(mode="after")
    def validate_threshold_order(self) -> "Settings":
        """Ensure low-threshold is strictly below high-threshold."""
        if self.completion_low_threshold >= self.completion_high_threshold:
            raise ValueError(
                "completion_low_threshold must be lower than completion_high_threshold"
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached settings object for dependency injection."""
    return Settings()
