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

## Доступ к базе для команды

База наружу не открыта: compose публикует её только на `127.0.0.1:5433` сервера (5432 на
хосте занят базой Ikanam). Участник ходит через SSH-туннель.

**Костя (raxlin4ik)** — выдано 2026-11-06:

- Linux-пользователь `kostya` без пароля и без шелла, ключ — с его GitHub
  (`github.com/raxlin4ik.keys`). В `authorized_keys` и в `/etc/ssh/sshd_config.d/70-kostya-db-tunnel.conf`
  ему разрешён только проброс на `127.0.0.1:5433`: ни команд, ни терминала, ни других портов.
- Роль PostgreSQL `kostya`: чтение и запись данных во всех таблицах `public`, без права
  создавать и менять таблицы — схема меняется только миграциями Alembic. Новые таблицы из
  миграций получают те же права автоматически (`ALTER DEFAULT PRIVILEGES FOR ROLE lms`).
  Не больше 5 соединений.
- Пароль роли лежит только на сервере: `/root/access/kostya-postgres.txt` (600). Передавать
  лично, не в чат проекта и не в репозиторий.

Подключение со своего компьютера:

```bash
ssh -N -L 5433:127.0.0.1:5433 kostya@195.24.71.184
```

и в другом окне (или в DBeaver / DataGrip: host `localhost`, port `5433`, db `lms`, user `kostya`):

```bash
psql -h localhost -p 5433 -U kostya -d lms
```

Отозвать: `userdel -r kostya`, удалить `70-kostya-db-tunnel.conf`, `systemctl reload ssh`,
в базе `REASSIGN OWNED BY kostya TO lms; DROP OWNED BY kostya; DROP ROLE kostya;`.

**Келли (kelleshh)** — выдано 2026-11-06, только этот проект:

- Linux-пользователь `kelli` без пароля, ключ — с её GitHub. Шелла на сервере нет:
  sshd (`/etc/ssh/sshd_config.d/71-kelli-mgimo-lms.conf`) любую команду отдаёт в
  `/usr/local/bin/mgimo-lms-ssh`, а та — через sudo в обёртку `/usr/local/bin/mgimo-lms`.
  В sudoers (`/etc/sudoers.d/mgimo-lms-kelli`) разрешена только эта обёртка.
- Обёртка ([scripts/server/mgimo-lms](../scripts/server/mgimo-lms), на сервер ставится
  руками root-ом) работает только с compose-проектом `/opt/mgimo-lms`: сервисы и модули по
  белому списку, каждый вызов пишется в журнал (`journalctl -t mgimo-lms`). В группу docker
  Келли не добавлена: это был бы root на всём VPS, включая Ikanam.
- При `deploy` обёртка не принимает архивы с символическими ссылками и выбрасывает из них
  `docker-compose*.yml` и `.env`: что монтируется с хоста, решает только root.

```bash
ssh kelli@195.24.71.184 help
ssh kelli@195.24.71.184 status
ssh kelli@195.24.71.184 logs web 100
DEPLOY_HOST=kelli@195.24.71.184 sh scripts/deploy.sh
ssh -t kelli@195.24.71.184 psql
```

Обновить обёртку: `scp scripts/server/mgimo-lms* root@…:/tmp/` и `install -m 755` в
`/usr/local/bin`. Отозвать: `userdel -r kelli`, удалить `71-kelli-mgimo-lms.conf` и
`/etc/sudoers.d/mgimo-lms-kelli`, `systemctl reload ssh`.

## Успеваемость и вымышленный контингент (решение 0006)

Миграция 0006 применяется сама при старте web (`alembic upgrade head` в Dockerfile). После
выкладки данные создаются один раз:

```bash
docker compose exec web python -m app.seed       # student1, student2, dean
docker compose exec web python -m app.generate   # ~3000 студентов, ведомости, баллы, teacher1/teacher2
```

Или со своего компьютера через туннель ролью `kostya` (схему она не меняет, данные — может):
в локальном `.env` `DATABASE_URL=postgresql+asyncpg://kostya:<пароль>@localhost:5433/lms` и
`DEMO_PASSWORD` — тот же, что на стенде (README), затем те же две команды без `docker compose exec web`.

Повторный запуск ничего не дублирует. `--reset` стирает сгенерированных студентов и **всю**
успеваемость, включая баллы, поставленные руками, — на стенде во время показа не запускать.

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
