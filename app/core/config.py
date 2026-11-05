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

    # Pyrus — система деканата (решение 0003). Логин и ключ — бота Pyrus: ими же
    # проверяется подпись входящего вебхука. Пустой логин = интеграция выключена.
    pyrus_auth_url: str = "https://accounts.pyrus.com/api/v4/auth"
    pyrus_login: str = ""
    pyrus_security_key: str = ""
    pyrus_form_id: int = 0
    pyrus_sync_interval: int = 300
    """Секунды между фоновыми сверками открытых заявок. 0 — сверка только вручную (CLI)."""

    cert_download_days: int = 30
    """Сколько дней после готовности студент может скачать PDF справки."""

    pdf_font_path: str = ""
    """TTF с кириллицей для PDF. Пусто — ищем DejaVuSans / Arial в системных путях."""

    @property
    def pyrus_enabled(self) -> bool:
        return bool(self.pyrus_login and self.pyrus_security_key and self.pyrus_form_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
