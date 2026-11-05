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
Регулярного обновления расписания пока нет.

## Подводные камни

- **Caddyfile монтируется в контейнер одним файлом.** Если файл на хосте заменить целиком
  (scp, редактор с сохранением через новый файл), контейнер продолжит видеть старый: `caddy
  reload` ответит «config is unchanged». Так и случилось при первой выкладке. Помогает
  `docker compose up -d --no-deps caddy` в `/opt/ikanam` — перерыв у schedule.ekanamfriends.ru
  несколько секунд, сертификаты лежат в volume и не перевыпускаются.
- **Выкладка Ikanam-schedule может стереть блок кабинета.** В репозитории бота `Caddyfile`
  без блока lms-university: если при его деплое `Caddyfile` скопировать из репозитория,
  кабинет пропадёт из Caddy. Блок нужно перенести и в репозиторий бота.
- Pyrus на стенде не настроен — заявки на справки копятся в очереди со статусом «Принята».
