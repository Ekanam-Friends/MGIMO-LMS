# Стенд на сервере

Демо: https://lms-university.ekanamfriends.ru. Сервер — общий VPS с ботом Ikanam-schedule
(`root@195.24.71.184`), как решено в [0001](decisions/0001-architecture.md).

## Как устроено

![Развёртывание на VPS: общий Caddy, отдельные сети и базы MGIMO-LMS и Ikanam, доступ команды к PostgreSQL через SSH-туннель.](diagrams/deployment.svg)

- Проект кабинета — `/opt/mgimo-lms`: свой compose, свой PostgreSQL (порт наружу не
  публикуется), свой `.env` (создан на сервере, в репозиторий не попадает).
- Общий у проектов только Caddy. Он подключён ко внешней docker-сети `edge`, в которой web
  кабинета виден под алиасом `lms-web`.
- В `/opt/ikanam` для этого добавлены (бэкапы рядом, `*.bak-2026-11-05`):
  - блок `lms-university.ekanamfriends.ru { … reverse_proxy lms-web:8000 }` в конце `Caddyfile`;
  - `caddy.networks: [default, edge]` и внешняя сеть `edge` в `docker-compose.override.yml`.

## Выкладка

Проверено 17 ноября 2026: на стенде код dev `0f57321` (после PR #43),
схема `0013_merge`. В публичном Chromium проверены три роли на 1440/360 px,
кнопка ассистента и сохранение недели при смене языка. Подробности и ограничения —
в [журнале инфраструктуры](prompts/infra.md#2026-11-17--деплой-после-pr-43).

> Если выполнено аварийное переключение БД по инструкции ниже, обычный `deploy.sh`
> и `docker compose up` запрещены до восстановления штатной топологии: они могут
> запустить старую primary. Используйте только полный набор файлов failover и `--no-deps`.

Перед изменением рабочей БД обязательна резервная копия PostgreSQL. Хранить не более трёх
последних полных копий; старые удалять после успешной проверки новой версии. Для этого
деплоя использован формат `pg_dump -Fc`, архив проверен командой `pg_restore --list`.
Серверные `.env`, `docker-compose.override.yml` и каталог `data` при выкладке сохраняются.

```bash
sh scripts/deploy.sh
```

Скрипт отправляет закоммиченный `HEAD` архивом по ssh (репозиторий приватный, сервер его не
клонирует) и пересобирает `web`. Миграции применяются при старте контейнера.

## Реплика PostgreSQL — задача #19

Проверено 16 ноября 2026: на `/opt/mgimo-lms` запущен `mgimo-lms-db-replica-1`,
PostgreSQL 16, асинхронный hot standby. Отдельный том `mgimo-lms_pgdata-replica`,
лимиты 384 МиБ RAM / 0,5 CPU, порт на хосте отсутствует. `web` и `bot` продолжают
использовать primary: маршрутизация чтений на реплику не реализована.

Установка из корня проекта на VPS (root, Python 3 и Docker Compose):

```bash
cd /opt/mgimo-lms
python3 scripts/setup_replica.py
sh scripts/server/check-replica.sh
```

Перед изменениями скрипт делает `pg_dump -Fc`, проверяет список архива через
`pg_restore --list`, сохраняет `pg_hba.conf` и `postgresql.auto.conf` в
`backups/replica-setup/<UTC>/`. Это проверка структуры архива, не пробное восстановление.
После успешного запуска остаётся максимум три набора копий. Пароль отдельной роли
`lms_replica` генерируется на сервере в `.secrets/replication-password` (0600),
не выводится и не коммитится. Роль имеет REPLICATION, без SUPERUSER/CREATEDB/CREATEROLE.
HBA разрешает ей репликацию из default-подсети проекта, с SCRAM-аутентификацией.

Настройка использует уже включённые `wal_level=replica`, `max_wal_senders=10` и
`max_replication_slots=10`; основная БД не перезапускается. Для слота
`mgimo_lms_replica` установлен `max_slot_wal_keep_size=512MB` (проверяется на checkpoint,
не является точным пределом размера всего WAL). При долгом простое реплики слот может
потерять нужный WAL: потребуется новая базовая копия, а не бесконечный рост диска.
Скрипт не очищает существующий или незавершённый том и не запускает promoted-том как standby.

Ожидаемая проверка: `state=streaming`, слот `active=t`, `lag_bytes` близко к нулю;
healthcheck реплики проверяет `pg_is_in_recovery()` и активный WAL receiver
со статусом `streaming`. Доступность порта сама по себе не подтверждает репликацию.
При недоступной primary проверка готовности возвращает отказ, процесс standby
при этом может продолжать обслуживать чтение. Скрипт проверки завершится ошибкой
и при ошибке SQL, неактивном слоте или отсутствии streaming.
на реплике `pg_is_in_recovery=t`, `read_only=on`. `lag_bytes` сравнивает актуальный LSN
основной БД с replay LSN, поэтому пригоден и при отсутствии прикладных записей.
Временные поля lag могут быть NULL при простое — это само по себе не сбой.

Проверенные сценарии: новая техническая запись появилась на реплике; попытка записи
на standby отклонена с SQLSTATE 25006; после перезапуска только реплики streaming
восстановился. Техническая таблица затем удалена на основной БД. Повторная установка
прошла, осталось три набора бэкапов. Проверены вход `student1` и расписание в браузере
на ширине 360 px; переключение боевого приложения не выполнялось.

### Ручное аварийное переключение

Реплика на том же VPS помогает при отказе процесса основной БД, но не при отказе
VPS/диска. Асинхронная репликация допускает потерю ещё не полученных транзакций.
Удаления и ошибочные изменения тоже реплицируются: отдельные резервные копии необходимы.
При перегрузке общей машины сначала оценить CPU, RAM и диск: promotion не добавляет ресурсов.

1. Остановить все записи и старую primary. Если она недоступна через Docker,
   сначала обеспечить её изоляцию; без этого promotion запрещён из-за split brain.

   ```bash
   cd /opt/mgimo-lms
   docker compose stop web bot db
   docker update --restart=no mgimo-lms-db-1
   ```

2. Посмотреть последний полученный и применённый WAL. При доступной primary до её
   остановки сравнить `pg_current_wal_lsn()` с replay LSN реплики командой проверки.
   После аварии равенство receive/replay не доказывает получение всех записей primary.

   ```bash
   docker exec mgimo-lms-db-replica-1 psql -U lms -d lms -c \
     'SELECT pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn();'
   docker exec mgimo-lms-db-replica-1 psql -U lms -d lms -c 'SELECT pg_promote(true, 60);'
   docker exec mgimo-lms-db-replica-1 psql -U lms -d lms -c 'SELECT pg_is_in_recovery();'
   ```

   Продолжать только если `pg_promote=t`, `pg_is_in_recovery=f`.

3. Применить подготовленный failover-файл. На этом сервере существует
   `docker-compose.override.yml`; он сохраняется в цепочке, чтобы не потерять сети,
   корпус и параметры бота. `--no-deps` обязателен: старую БД запускать нельзя.

   ```bash
   dc_failover() {
     docker compose -f docker-compose.yml -f docker-compose.override.yml \
       -f docker-compose.replica.yml -f docker-compose.failover.yml "$@"
   }
   dc_failover up -d --no-deps --wait db-replica
   dc_failover up -d --no-deps web bot
   dc_failover ps
   ```

4. Проверить вход тестовым студентом, расписание и создание тестовой заявки;
   проверить логи web/bot. До восстановления топологии все операции запуска делать
   через `dc_failover ... --no-deps`. Старую primary не включать: она не содержит
   новые записи и после promotion принадлежит другой ветви WAL.

Возврат требует отдельного восстановления: сохранить копию новой primary,
пересоздать старую БД как standby от неё (`pg_basebackup` либо проверенный `pg_rewind`),
проверить догон репликации и выполнить согласованное переключение с остановкой записей.
Простое удаление failover-файла и запуск старого тома запрещены. Это не автоматический failover;
на рабочем приложении аварийное переключение пока не испытывалось.

17 ноября 2026 сценарий установки и переключения прошёл на отдельной одноразовой
паре PostgreSQL 16. Проверены доставка записи, read-only, четыре установки
с сохранением трёх последних бэкапов, перезапуск standby, отказ healthcheck
при остановке primary, восстановление streaming, promotion и пересоздание
контейнера с failover-compose; новая primary принимает запись. Тест запускается
через `RUN_REPLICA_TESTS=1 pytest -q tests/test_replica_setup.py`, создаёт свой
проект Compose с уникальным префиксом и удаляет только его контейнеры и тома.
Это не проверка переключения приложения на рабочем VPS.

Источники: [streaming replication PostgreSQL 16](https://www.postgresql.org/docs/16/warm-standby.html),
[pg_basebackup](https://www.postgresql.org/docs/16/app-pgbasebackup.html),
[ограничение WAL для слотов](https://www.postgresql.org/docs/16/runtime-config-replication.html).

Данные:

```bash
ssh root@195.24.71.184
cd /opt/mgimo-lms
docker compose exec web python -m app.schedule.import_ruz --full --group 24МЖ-02 --group 25МО-24
docker compose exec web python -m app.seed
```

На стенде импортированы только две группы демо-студентов (1987 занятий за семестр, 10 секунд).
Регулярно расписание обновляет бот: раз в `BOT_IMPORT_HOURS` часов по группам подключённых студентов.

Чтобы расписание на пустой базе заполнилось само ([решение 0011](decisions/0011-schedule-bootstrap.md)),
в серверном `.env` задайте `BOT_BOOTSTRAP_GROUPS=400` и перезапустите бота (`docker compose up -d bot`):
через ~2 минуты он начнёт догружать весь семестр недостающих групп. После подтверждения
покрытия текущего семестра повторные старты РУЗ не трогают. Пустые ответы и ошибки
не расходуют цель; время зависит от количества попыток и доступности источника.
`BOT_IMPORT_SCOPE=students` — обновлять раз в `BOT_IMPORT_HOURS` группы всех студентов, а не только с Telegram.

Перед первым запуском новой версии необходима миграция `0010` (через web): она добавляет
подтверждение покрытия семестра по каждой группе ([0012](decisions/0012-schedule-semester-coverage.md)).
Перед изменением рабочей БД сохранить `pg_dump`; хранить не более трёх последних копий.
Исторические импорты без подтверждения перечитываются один раз. Пустые группы не считаются
загруженными; если непустых групп недостаточно, в журнале будет предупреждение.

### Состояние после деплоя 16 ноября 2026

- **Код:** `dev`, merge-коммит PR #35 `3d2106d2bf4aab23c79c344c3285b8905fc8f8f4`.
  Web и bot пересобраны и запущены около 19:44 МСК. Бот начал Telegram polling.
- **PostgreSQL:** версия 16.15, healthy; миграции `0007 → 0008 → 0009 → 0010`
  применены при запуске web, `alembic current` показывает `0010 (head)`.
- **Данные:** 2 группы с расписанием, 1987 занятий, 26 студентов; таблица подтверждений
  покрытия семестра пуста. Seed, генератор и массовый импорт при деплое не запускались.
- **Настройки бота:** `BOT_BOOTSTRAP_GROUPS=0`, `BOT_IMPORT_SCOPE=linked`,
  `BOT_IMPORT_HOURS=6`; бот подключён к своей БД на `mgimo-lms-db-1`.
- **Проверка снаружи:** вход student1, главная и расписание текущей недели на 360 px;
  `innerWidth=360`, `scrollWidth=345`. [Скриншот после деплоя](process/screenshots/2026-11-16-pr35-deployed-360.jpg).
- **Бэкап до миграций:** `/opt/mgimo-lms/backups/lms-20261116T164050Z-before-pr35.dump`,
  235442 байта, формат custom, `pg_restore --list` успешен. В каталоге оставлены три
  полные копии БД. Исходный код до выкладки сохранён в `release-20261116T164050Z-before-pr35.tar.gz`,
  прежние образы — под тегами `mgimo-lms-web:rollback-pr35` и `mgimo-lms-bot:rollback-pr35`.

**Ограничение источника:** повторный HTTPS-запрос из нового контейнера бота к справочнику
факультетов РУЗ получает `CERTIFICATE_VERIFY_FAILED: certificate has expired`.
Проверку TLS не отключали; существующее расписание доступно из базы. Исправление bootstrap
не устраняет ошибку сертификата источника, поэтому догрузка на 400 групп пока не включена.

При откате сначала сохранить новое состояние БД. Старые образы можно использовать только
с учётом совместимости схемы; полный возврат к состоянию до выкладки требует восстановления
проверенной резервной копии и кода. Автоматического отката БД при ошибке приложения нет.

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

## Кэш Redis — задача #24

Решение [0027](decisions/0027-read-cache.md): общий кэш web и бота — расписание, лента
колокольчика, повторные ответы ассистента. Импорт РУЗ в боте сбрасывает кэш web через Redis.
**На стенде ещё не включён** (на 21 ноября 2026); включается обычной выкладкой:

1. В `docker-compose.yml` уже есть сервис `redis` (redis:7-alpine, `maxmemory 64mb`,
   `allkeys-lru`, без записи на диск, `mem_limit 96m`, порт наружу не публикуется) и
   `CACHE_URL=redis://lms-redis:6379/0` у `web` и `bot`. В серверный `.env` ничего добавлять
   не нужно: `environment` в compose перекрывает `.env`. Если в серверном
   `docker-compose.override.yml` у `web`/`bot` переопределён `environment`, проверить,
   что `CACHE_URL` там не затёрт пустым значением.
2. Проверить запас памяти до выкладки: `free -m` (Redis займёт до ~70 МБ).
3. `sh scripts/deploy.sh` — `docker compose up -d --build web` сам поднимет `redis`
   (зависимость `depends_on`), затем пересоздаётся `bot`.
4. Проверка:
   ```bash
   docker compose ps redis                                   # Up (healthy)
   docker compose exec redis redis-cli ping                  # PONG
   docker compose exec web python -c "from app.core.config import get_settings; print(get_settings().cache_url)"
   # открыть главную студентом, затем:
   docker compose exec redis redis-cli --scan --pattern 'lms:v1:*' | head
   docker compose logs --since 10m web bot | grep 'кэш недоступен'   # пусто
   ```
5. Имя `lms-redis`, а не `redis`, — потому что бот на сервере подключён ещё и к сети
   `ikanam_default` (прокси Telegram): короткое имя могло бы указать на чужой сервис,
   как уже было с `db`.

Откат: `CACHE_URL=` в `environment` серверного override для `web` и `bot` и
`docker compose up -d web bot` — кабинет вернётся к кэшу в памяти процесса. Падение или
остановка `redis` сайт не роняет: ошибки кэша логируются, запросы идут в БД
(«кэш недоступен …» в логах web). Сбросить весь кэш: `docker compose restart redis`
(данные на диск не пишутся). При аварийном переключении на реплику с `--no-deps`
`redis` нужно поднять отдельно: `docker compose up -d redis`.

## Корпус для ИИ-ассистента (RAG)

Решение [0007](decisions/0007-rag-assistant-infra.md). Это делает **root** один раз; Келли
после этого работает через `deploy`, `run app.<модуль>` и `corpus`.

Состояние на 2026-11-10: шаги 1 и 2 выполнены (папка `/opt/mgimo-lms/data/regulations` с 27 PDF и
`manifest.csv`, монтирование `/data` в `web` и `bot`, команда `corpus` в обёртке). Не сделаны шаги 3 и 4:
ключей LLM и сертификата на сервере пока нет, выход к провайдеру не проверен.

1. Папка корпуса. Контейнеры работают под uid 1000 (`lms`), поэтому папка принадлежит ему:

   ```bash
   mkdir -p /opt/mgimo-lms/data/regulations /opt/mgimo-lms/data/ca
   chown -R 1000:1000 /opt/mgimo-lms/data
   ```

2. Монтирование. В серверный `/opt/mgimo-lms/docker-compose.override.yml` (он уже есть:
   там сети и `DATABASE_URL` для бота) **добавить**, не заменяя файл целиком:

   ```yaml
   services:
     web:
       volumes:
         - /opt/mgimo-lms/data:/data
     bot:
       volumes:
         - /opt/mgimo-lms/data:/data
   ```

   Применить: `cd /opt/mgimo-lms && docker compose up -d --no-deps web bot`.

3. Ключи — в `/opt/mgimo-lms/.env` (имена — в `.env.example`, блок «ИИ-ассистент»). Значения
   передаются лично, не в чат и не в репозиторий. Для GigaChat корневой сертификат Минцифры
   кладётся в `/opt/mgimo-lms/data/ca/`, путь к нему — `GIGACHAT_CA_BUNDLE=/data/ca/<файл>`.
   После правки `.env`: `docker compose up -d --no-deps web bot` (`restart` переменные не перечитывает).

4. Проверка выхода к провайдеру из контейнера (до написания кода ассистента):

   ```bash
   docker compose exec web python -c "import urllib.request as u; print(u.urlopen('https://ngw.devices.sberbank.ru:9443', timeout=10).status)"
   ```

   Ответ с кодом (даже 4xx) — сеть есть; ошибка сертификата — нужен `GIGACHAT_CA_BUNDLE`;
   таймаут — доступа нет, нужен прокси (как для Telegram). Для YandexGPT адрес —
   `https://llm.api.cloud.yandex.net`.

   Результат 2026-11-10 из контейнера `web`: GigaChat (`ngw.devices.sberbank.ru:9443`,
   `gigachat.devices.sberbank.ru`) достижим, но `CERTIFICATE_VERIFY_FAILED` — нужен корневой сертификат
   Минцифры в `data/ca/` и `GIGACHAT_CA_BUNDLE`; YandexGPT достижим (404 на корне — норма);
   `mgimo.ru` отвечает 200, но это страница-проверка, не документ.

5. Файлы с mgimo.ru Келли кладёт в `/data/regulations` (внутри контейнера) или скачивает
   скриптом через `run app.<модуль>`. Корпус в git не попадает (`/data/` в `.gitignore`).

`pgvector` на старте не нужен. Если понадобится, образ `db` менять нельзя поверх старого тома
(см. «Риски» в 0007): сначала `pg_dump` в `/opt/mgimo-lms/backups/`.

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
ssh kelli@195.24.71.184 corpus ls                      # корпус положений для ассистента
ssh kelli@195.24.71.184 corpus put x.pdf < x.pdf        # записать файл (до 50 МБ, плоская папка)
ssh kelli@195.24.71.184 corpus rm x.pdf                 # удалить файл корпуса
```

`corpus` работает только с `/opt/mgimo-lms/data/regulations` (решение [0007](decisions/0007-rag-assistant-infra.md)):
имена по белому списку, ссылок и вложенных путей нет. Файл больше 50 МБ команда отклоняет с ошибкой.

Обновить обёртку: `scp scripts/server/mgimo-lms* root@…:/tmp/` и `install -m 755` в
`/usr/local/bin`. Отозвать: `userdel -r kelli`, удалить `71-kelli-mgimo-lms.conf` и
`/etc/sudoers.d/mgimo-lms-kelli`, `systemctl reload ssh`.

## Успеваемость и вымышленный контингент (решение 0007)

Миграция 0007 применяется сама при старте web (`alembic upgrade head` в Dockerfile). После
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
- Параметры Pyrus на стенде настроены (проверено 2026-11-16 без вывода секретов).
  Полный цикл внешнего согласования при этом деплое не проверялся.

## Проверенный релиз 17 ноября 2026

На стенд выложен `dev` d119642adb991bd2e292d2168b4ad12da2dbb0e3 после PR #36–#41.
Схема primary — `0013_merge`; web и bot используют один проверенный образ с меткой
ревизии. До применения бэкап восстановлен на отдельном PostgreSQL 16 и прошёл
миграции и `alembic check`. Реплика healthy, WAL streaming; автоматическая
проверка Chromium: 26 открытий страниц трёх ролей при 360/1440 px без ошибок.
Секреты, серверный override, корпус и OCR-контейнер сохранены. Качество ответов
GigaChat и экспертная приёмка OCR этим деплоем не подтверждаются.
Резервная копия: `/opt/mgimo-lms/backups/deploy-20261117T003700Z/lms.dump`.
Подробнее: [журнал процесса](prompts/infra.md).
