# 0028. Манифесты Kubernetes для кабинета (k3s / minikube)

Дата: 21 ноября 2026. Статус: принято; **в живом кластере не запускалось, проверено статически**.
Постановщик: Legpopov, задача [#25](https://github.com/Ekanam-Friends/MGIMO-LMS/issues/25);
исполнитель — raxlin4ik с ИИ (Claude Code). Инструкция — [docs/kubernetes.md](../kubernetes.md).

Номер 0028, а не 0027: параллельно идёт задача про кэш (#24), которой оставлен следующий
свободный номер.

## Контекст

Стенд кабинета работает на docker compose на общем VPS (решение [0001](0001-architecture.md),
[docs/deploy.md](../deploy.md)) и остаётся основным. Задача #25 — показать, что тот же образ
разворачивается в Kubernetes одной командой, с секретами вне репозитория. Ограничения,
которые определили решения ниже, — из кода приложения:

- `app/main.py`: фоновая сверка заявок с Pyrus и обработчик загруженных документов
  запускаются внутри процесса web и рассчитаны на **один** процесс.
- `Dockerfile`: CMD делает `alembic upgrade head` перед uvicorn — безопасно лишь при одном
  экземпляре; образ работает под uid 1000 (`lms`).
- `app/bot/main.py`: бот — long polling; второй экземпляр с тем же токеном получает 409 Conflict.
- Приложение пишет на диск: снимок корпуса `chunks.jsonl` в `RAG_CORPUS_DIR`
  (`app/assistant/documents.py`, атомарная замена через временный файл рядом) и временные
  файлы multipart-загрузок (PDF до 50 МБ) в `/tmp`. PDF справок и загруженные документы
  хранятся в базе.
- Пустое значение числовой настройки (`PYRUS_FORM_ID=`) не проходит проверку pydantic.

## Решение

Каталог `deploy/k8s/`, kustomize (встроен в kubectl, отдельной установки не нужно):

| Часть | Как | Почему |
|-------|-----|--------|
| `base/` | Namespace `mgimo-lms`; web, bot, redis, Ingress; ConfigMap `lms-config` из `config.env` | Общее для всех вариантов. Хэш в имени ConfigMap перезапускает поды при изменении настроек |
| `postgres/` | PostgreSQL 16 (alpine): StatefulSet, том 5 ГиБ, headless Service, pg_isready-пробы, NetworkPolicy | Подключается только overlay-ями с базой в кластере |
| `overlays/k3s` | base + postgres, Ingress через Traefik | Вариант по умолчанию |
| `overlays/minikube` | base + postgres, Ingress через ingress-nginx с лимитом тела 60 МБ | ingress-nginx по умолчанию режет загрузки до 1 МБ |
| `overlays/external-db` | только base; `DATABASE_URL` из `.env` | «Внешняя БД» без PostgreSQL в кластере |

**web.** Deployment, `replicas: 1`, `strategy: Recreate`. Две реплики дублировали бы сверку
Pyrus и обработку документов, а том корпуса — ReadWriteOnce. Recreate даёт несколько секунд
простоя при обновлении (как `docker compose up -d`), зато старый и новый процесс не работают
одновременно. Масштабирование web требует сначала вынести фоновые задачи в отдельный процесс
(аналог бота) — это вне #25.

**Миграции — initContainer `migrate`** (`alembic upgrade head` с повторами, пока база
поднимается), основной контейнер запускает uvicorn без alembic. Почему не Job: спецификация
Job неизменяема, её пришлось бы удалять и пересоздавать при каждой выкладке и отдельно
заставлять web ждать её завершения; при одной реплике web initContainer даёт тот же порядок
«сначала схема, потом код» без гонок, а ошибка видна как `Init:Error` с логами
`kubectl logs deploy/web -c migrate`. Бот ничего не мигрирует: его initContainer ждёт,
пока `alembic current` покажет `(head)`.

**bot.** Deployment, ровно одна реплика, Recreate, без Service и без HTTP-проб (их нечем
отвечать; упавший процесс перезапускает kubelet). Пустой `TELEGRAM_BOT_TOKEN` —
`scripts/k8s-up.sh` ставит `replicas: 0`, как `deploy.sh` не поднимает бота в compose.

**redis.** `redis:7-alpine`, ClusterIP без публикации наружу, `--maxmemory 64mb
--maxmemory-policy allkeys-lru`, без записи на диск (кэш), лимит пода 128 МиБ, NetworkPolicy —
только web и bot. `CACHE_URL=redis://redis:6379/0` передаётся web и bot для задачи #24; пока код
её не читает, настройки её игнорируют (`extra="ignore"`).

**Ingress.** Только web, хост — параметр (`K8S_HOST`, по умолчанию `lms.local`). TLS
необязателен: при `K8S_TLS_SECRET` добавляется блок `tls`, `PUBLIC_BASE_URL` становится
https, `COOKIE_SECURE=true`. При доступе через port-forward внешний адрес задаётся
`K8S_PUBLIC_URL` (например `http://localhost:8000`), иначе ссылки-подписки на календарь ведут на
`lms.local`; `COOKIE_SECURE` выводится из схемы итогового адреса. Без TLS `COOKIE_SECURE=false` — иначе браузер не сохранит cookie
сессии на http и вход не сработает.

**Секреты.** В репозитории их нет — ни значений, ни объектов `Secret` (это проверяет
валидатор). `scripts/k8s-up.sh` читает `.env` и раскладывает его по списку
`deploy/k8s/env-keys.txt`. Список **закрыт по умолчанию**: в ConfigMap `lms-env` попадают только
ключи, явно помеченные несекретными (`config`, `envonly`); секретные (пароли, токены, ключи API,
соль имён, прокси) и любые неописанные — в Secret `lms-secrets` командой
`kubectl create secret generic --from-env-file`, для неописанных печатается предупреждение.
Временные env-файлы создаются через `mktemp -d` вне репозитория, поэтому не попадают в контекст
`docker build` (дополнительно `deploy/k8s/.build` и `deploy/k8s/secrets.env` в `.dockerignore`). Порядок в контейнере: `lms-config` → `lms-env` → `lms-secrets`,
поздний побеждает, поэтому `.env` перекрывает значения по умолчанию. Ключи `managed`
(адрес базы в кластере, путь корпуса, `CACHE_URL`, `PUBLIC_BASE_URL`, `COOKIE_SECURE`) задаёт
кластер, значение из `.env` для них игнорируется. Пустые значения не передаются (pydantic
не разбирает `PYRUS_FORM_ID=`). Для базы в кластере `DATABASE_URL` собирается из
`POSTGRES_PASSWORD`, поэтому пароль ограничен символами, безопасными в URL. Контрольная сумма
`.env` пишется в аннотацию шаблона пода: изменили секрет — web и bot перезапустятся, не
меняли — повторный запуск ничего не трогает.

**Хранилище.** Класс по умолчанию кластера (local-path в k3s, standard в minikube): том
PostgreSQL 5 ГиБ (volumeClaimTemplate) и том корпуса `lms-corpus` 2 ГиБ. `k8s-down.sh` без
`--purge` тома не удаляет.

**Безопасность подов.** Не root (web/bot — uid 1000 как в Dockerfile, postgres — 70,
redis — 999), `allowPrivilegeEscalation: false`, `capabilities: drop ALL`, seccomp
RuntimeDefault, токен ServiceAccount не монтируется. У web, bot и redis корень только для
чтения; записываемые места — тома: `/tmp` (emptyDir) и `/data/regulations` (PVC).
У PostgreSQL корень записываемый — так устроен официальный образ.
Requests/limits заданы всем контейнерам.

**Образ.** Собирается из того же Dockerfile, в реестр не публикуется: импорт в containerd k3s
(`k3s ctr images import`) или `minikube image load`, `imagePullPolicy: Never`. Тег — коммит git
(с любыми незакоммиченными или неотслеживаемыми файлами — с меткой времени), поэтому одинаковый
код не пересобирается, а новый файл в `app/` не останется вне образа. Сборка — только docker
(или `minikube image build`): nerdctl на голом k3s не видит его containerd и без buildkitd не собирает.

**Одна команда.** `sh scripts/k8s-up.sh` (POSIX sh) проверяет kubectl, кластер и `.env`,
собирает и загружает образ, создаёт Secret и ConfigMap, делает `kubectl apply -k` временной
kustomization (`deploy/k8s/.build/`, в .gitignore, с `namespace: mgimo-lms`, чтобы merge над
`lms-config` из base находил объект) с тегом, хостом и числом реплик бота,
ждёт rollout и печатает адрес. `sh scripts/k8s-down.sh` удаляет нагрузки, сохраняя данные;
`--purge` — namespace целиком после подтверждения.

**Проверка без кластера.** `scripts/k8s-validate.py` (нужен только PyYAML; запускается
тестом `tests/test_k8s_manifests.py` и workflow `.github/workflows/k8s-validate.yml`)
собирает каждый overlay упрощённым kustomize и проверяет ссылки kustomization, селекторы
и порты Service, Ingress, PVC, ConfigMap/Secret, пробы, ресурсы, securityContext, реплики,
отсутствие секретов, покрытие переменных `.env.example` и совпадение имён с `Settings`;
при установленном `kubernetes-validate` — строгие JSON-схемы API 1.29 и 1.33. Тест
дополнительно запускает `k8s-up.sh` с заглушкой kubectl.

## Чего не делали

- Compose-стенд на VPS не менялся и остаётся рабочим вариантом. Реплика PostgreSQL
  (решение [0014](0014-postgresql-replica.md)) в Kubernetes не переносилась.
- Нет Helm, cert-manager, реестра образов, HPA, бэкапов базы в кластере.
- Корпус положений и сертификат GigaChat в образ не входят: их переносят руками
  (`kubectl cp`, Secret `lms-gigachat-ca`), см. инструкцию.

## Ограничения и риски

- **В живом кластере не запускалось** (на машине разработчика нет docker/kubectl/k3s).
  Проверено статически: разбор YAML, собственная сборка overlay, JSON-схемы Kubernetes,
  `sh -n`, прогон скрипта с заглушками. Реальный `kustomize build` и выкатку проверит
  капитан на сервере по чек-листу из [docs/kubernetes.md](../kubernetes.md).
- Упрощённый kustomize валидатора поддерживает только используемые поля; расхождение
  с настоящим kustomize возможно (например, при новых видах патчей).
- `readOnlyRootFilesystem` для web/bot выбран по разбору кода; если какая-то библиотека
  захочет писать в домашний каталог, это проявится ошибкой «Read-only file system».
- PostgreSQL под uid 70 рассчитывает, что провайдер томов создаёт каталог, доступный на запись
  (local-path и standard так делают).
- NetworkPolicy соблюдается в k3s, в minikube с CNI по умолчанию — нет.
- Пароль базы, сменённый в `.env` после первой инициализации тома, в самой базе не меняется.
- Запуск k3s на том же VPS, что compose, потребует портов 80/443, занятых общим Caddy —
  это отдельное решение, в #25 не принималось.
