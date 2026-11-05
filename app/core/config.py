"""Настройки из окружения и `.env`. Секретов со значениями по умолчанию здесь нет."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str

    ruz_base_url: str = "https://ruz.mgimo.ru/api/"
    ruz_request_delay: float = 0.5
    ruz_concurrency: int = 2
    ruz_timeout: float = 120.0

    fake_name_salt: str = "mgimo-lms"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
