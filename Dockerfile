# Образ кабинета: FastAPI + Jinja2. Один процесс uvicorn (фоновая сверка с Pyrus рассчитана
# на один процесс, см. app/main.py).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# DejaVu — кириллический шрифт для PDF справок (PDF_FONT_PATH по умолчанию его и ищет).
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Зависимости отдельным слоем: правка кода не переустанавливает их.
COPY pyproject.toml ./
# Ставим только зависимости: сам пакет удаляем, код берётся из /app (рабочий каталог),
# вместе с шаблонами и статикой.
RUN mkdir app && touch app/__init__.py && pip install '.[rag]' && pip uninstall -y mgimo-lms && rm -rf app build

COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./

RUN useradd --create-home --uid 1000 lms && mkdir -p /data/regulations \
    && chown -R lms:lms /app /data/regulations
USER lms

EXPOSE 8000
# Миграции при старте: один экземпляр web, гонки нет.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]
