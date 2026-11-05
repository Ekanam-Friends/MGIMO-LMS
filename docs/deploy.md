# Стенд на сервере

Демо: https://lms-university.ekanamfriends.ru. Сервер — общий VPS с ботом Ikanam-schedule
(`root@195.24.71.184`), как решено в [0001](decisions/0001-architecture.md).

## Как устроено

```
интернет ──443──> ikanam-caddy-1 (/opt/ikanam, общий Caddy)
                     │ schedule.ekanamfriends.ru      → web:8000      (сеть ikanam_default)
                     │ lms-university.ekanamfriends.ru → lms-web:8000 (сеть edge)
                     ▼
              mgimo-lms-web-1 ── mgimo-lms-db-1   (/opt/mgimo-lms, своя сеть, своя БД)
```

- Проект кабинета — `/opt/mgimo-lms`: свой compose, свой PostgreSQL (порт наружу не
  публикуется), свой `.env` (создан на сервере, в репозиторий не попадает).
- Общий у проектов только Caddy. Он подключён ко внешней docker-сети `edge`, в которой web
  кабинета виден под алиасом `lms-web`.
- В `/opt/ikanam` для этого добавлены (бэкапы рядом, `*.bak-2026-11-05`):
  - блок `lms-university.ekanamfriends.ru { … reverse_proxy lms-web:8000 }` в конце `Caddyfile`;
  - `caddy.networks: [default, edge]` и внешняя сеть `edge` в `docker-compose.override.yml`.

## Выкладка

```bash
sh scripts/deploy.sh
```

Скрипт отправляет закоммиченный `HEAD` архивом по ssh (репозиторий приватный, сервер его не
клонирует) и пересобирает `web`. Миграции применяются при старте контейнера.

Данные:

```bash
ssh root@195.24.71.184
cd /opt/mgimo-lms
docker compose exec web python -m app.schedule.import_ruz --full --group 24МЖ-02 --group 25МО-24
docker compose exec web python -m app.seed
```

На стенде импортированы только две группы демо-студентов (1987 занятий за семестр, 10 секунд).
Регулярно расписание обновляет бот: раз в `BOT_IMPORT_HOURS` часов по группам подключённых студентов.

## Telegram-бот

Контейнер `bot` (решение [0005](decisions/0005-telegram-bot.md)), long polling — в Caddy ничего
добавлять не нужно. Один раз в `/opt/mgimo-lms/.env` на сервере:

```
TELEGRAM_BOT_TOKEN=<от @BotFather>
TELEGRAM_BOT_USERNAME=Lms_university_ekanamfriends_bot
PUBLIC_BASE_URL=https://lms-university.ekanamfriends.ru
```

`scripts/deploy.sh` поднимает `bot`, только если токен задан. Логи: `docker compose logs -f bot`.

**api.telegram.org с этого VPS напрямую недоступен** (проверено 2026-11-05: curl не соединяется).
Бот ходит через socks5-прокси проекта Ikanam — контейнер `proxy` в сети `ikanam_default`.
Для этого на сервере (не в репозитории) лежит `/opt/mgimo-lms/docker-compose.override.yml`,
который подключает к `ikanam_default` только сервис `bot`, а в `.env` —
`TELEGRAM_PROXY=socks5://proxy:1080`. Сам Ikanam не менялся. В `ikanam_default` есть и сервис
`db` (база Ikanam), поэтому override переопределяет боту `DATABASE_URL` на хост
`mgimo-lms-db-1` — иначе бот уходит в чужую базу. Если прокси Ikanam переедет или
упадёт, наш бот потеряет связь с Telegram вместе с ним.
Демонстрация пуша: `docker compose exec bot python -m app.bot.demo_change --login student1`.

## Подводные камни

- **Caddyfile монтируется в контейнер одним файлом.** Если файл на хосте заменить целиком
  (scp, редактор с сохранением через новый файл), контейнер продолжит видеть старый: `caddy
  reload` ответит «config is unchanged». Так и случилось при первой выкладке. Помогает
  `docker compose up -d --no-deps caddy` в `/opt/ikanam` — перерыв у schedule.ekanamfriends.ru
  несколько секунд, сертификаты лежат в volume и не перевыпускаются.
- **Блок кабинета живёт и в репозитории бота** (`lkanam-schedule/Caddyfile`, коммит `b33f3b0`),
  поэтому деплой бота его не сотрёт. Подключение caddy к сети `edge` — только в серверном
  `docker-compose.override.yml`: его при деплое бота не перезаписывать.
- **Сборка образа на сервере медленная** (PyPI ~50 КБ/с, несколько минут), и ssh из
  `deploy.sh` может оборваться посреди неё. Тогда сборку запускать на сервере отвязанно:
  `nohup sh -c "docker compose up -d --build web && docker compose up -d --build bot" > /tmp/mgimo-deploy.log 2>&1 &`.
- Pyrus на стенде не настроен — заявки на справки копятся в очереди со статусом «Принята».
