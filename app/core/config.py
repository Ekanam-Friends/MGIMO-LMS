"""Настройки из окружения и `.env`. Секретов со значениями по умолчанию здесь нет."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr
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

    session_days: int = 14
    """Сколько живёт сессия входа в кабинет."""
    cookie_secure: bool = True
    """Флаг Secure у cookie сессии. False — только для локального запуска по http."""

    demo_password: str = ""
    """Пароль тестовых учёток, которые создаёт `python -m app.seed`. Пусто — seed откажется."""

    # Telegram-бот (решение 0005). Токен — только из .env. Пустой токен = бот не стартует,
    # а кабинет прячет кнопку «Подключить Telegram».
    telegram_bot_token: str = ""
    telegram_bot_username: str = ""
    """Имя бота без @ — для ссылки t.me/<имя>?start=<код> из профиля."""
    telegram_proxy: str = ""
    """http:// или socks5:// — если с сервера api.telegram.org доступен с перебоями."""
    public_base_url: str = "http://localhost:8000"
    """Адрес кабинета снаружи: из него собирается ссылка-подписка на календарь."""
    telegram_link_code_minutes: int = 15
    """Сколько живёт одноразовый код привязки из профиля."""
    bot_import_hours: int = 6
    """Как часто бот перечитывает РУЗ по группам подключённых студентов (0 — не перечитывает)."""

    # Ассистент: публичные документы и минимальные данные кабинета; оценки после согласия.
    llm_provider: str = ""
    gigachat_auth_key: SecretStr = SecretStr("")
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat-2"
    gigachat_translation_model: str = "GigaChat-2-Max"
    gigachat_ca_bundle: str = ""
    llm_embedding_model: str = ""
    rag_use_embeddings: bool = False
    assistant_demo_student_access: bool = False
    """Только для базы вымышленных студентов: выбор любого студента преподавателем и сотрудником."""
    rag_corpus_dir: str = "/data/regulations"
    rag_top_k: int = Field(default=5, ge=1, le=6)
    rag_lexical_weight: float = Field(default=1., gt=0, le=10, allow_inf_nan=False)
    rag_semantic_weight: float = Field(default=1., gt=0, le=10, allow_inf_nan=False)
    rag_max_source_age_days: int = Field(default=30, ge=1, le=365)
    rag_requests_per_minute: int = Field(default=6, ge=1, le=30)
    rag_concurrent_requests: int = Field(default=1, ge=1, le=4)

    @property
    def bot_link_enabled(self) -> bool:
        return bool(self.telegram_bot_username)

    def feed_url(self, token: str) -> str:
        return f"{self.public_base_url.rstrip('/')}/feed/{token}.ics"

    def webcal_url(self, token: str) -> str:
        return "webcal://" + self.feed_url(token).split("://", 1)[1]

    @property
    def pyrus_enabled(self) -> bool:
        return bool(self.pyrus_login and self.pyrus_security_key and self.pyrus_form_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
