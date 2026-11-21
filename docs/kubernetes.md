# Кабинет в Kubernetes (k3s / minikube)

Решение — [0028](decisions/0028-kubernetes-manifests.md), задача #25. Основной стенд по-прежнему
работает на docker compose ([deploy.md](deploy.md)); Kubernetes — второй способ поднять тот же
кабинет, ничего в compose не меняя.

> **Статус: в живом кластере не запускалось, проверено статически; первый запуск — на сервере
> капитаном.** Что именно проверено — в конце документа. Перед первым запуском пройдите
> [чек-лист](#чек-лист-первого-запуска).

## Если вы не знаете Kubernetes — 5 слов

- **Кластер** — одна или несколько машин, на которых Kubernetes запускает контейнеры.
  k3s и minikube — два способа получить кластер на одной машине.
- **Под** — запущенный контейнер (иногда несколько) с вашим образом. Упал — Kubernetes
  перезапустит.
- **Deployment / StatefulSet** — «держи запущенными N подов из такого образа». StatefulSet —
  то же для базы данных: со своим постоянным диском.
- **Service** — постоянное имя внутри кластера (`postgres`, `redis`, `web`), по которому поды
  находят друг друга. **Ingress** — вход снаружи по домену (здесь только web).
- **Secret / ConfigMap** — переменные окружения: секретные и обычные.

Всё кабинетное лежит в одном **namespace** `mgimo-lms`: `kubectl -n mgimo-lms get all` показывает всё сразу.

## Что разворачивается

```
deploy/k8s/
  env-keys.txt          какие переменные .env несекретные (config, envonly), секретные (secret),
                        какие задаёт кластер (managed); неописанные — в Secret
  secrets.env.example   шаблон отдельного файла секретов (обычно не нужен — берётся .env)
  base/                 общее: namespace, web, bot, redis, Ingress, config.env -> ConfigMap lms-config
  postgres/             PostgreSQL 16 с постоянным томом
  overlays/k3s/         base + postgres, Ingress через Traefik (по умолчанию)
  overlays/minikube/    base + postgres, Ingress через ingress-nginx
  overlays/external-db/ base без postgres: база снаружи, DATABASE_URL из .env
scripts/k8s-up.sh       поднять / обновить одной командой
scripts/k8s-down.sh     остановить (данные остаются) или удалить всё (--purge)
scripts/k8s-validate.py проверка манифестов без кластера
```

| Под | Сколько | Зачем |
|-----|---------|-------|
| `postgres-0` | 1 | база, том 5 ГиБ |
| `web-…` | **1** | кабинет. Ровно одна копия: фоновая сверка Pyrus и обработка документов живут в процессе web. Перед стартом initContainer `migrate` применяет миграции |
| `bot-…` | 1 или 0 | Telegram-бот, long polling. 0 — если `TELEGRAM_BOT_TOKEN` пуст |
| `redis-…` | 1 | кэш для задачи #24 (`CACHE_URL=redis://redis:6379/0`), наружу не виден |

## Шаг 1. Кластер и инструменты

Нужны: `kubectl`, кластер (k3s **или** minikube) и `docker` для сборки образа. Для minikube без
docker скрипт соберёт образ внутри minikube (`minikube image build`). nerdctl на голом k3s не
подходит: он не видит containerd k3s и без buildkitd не собирает — поэтому скрипт его не использует.
Если docker на узле k3s ставить нельзя, соберите образ на другой машине, `docker save` → скопируйте →
`sudo k3s ctr images import <файл>` и запустите скрипт с `K8S_SKIP_BUILD=1 K8S_IMAGE_TAG=<тег>`.

### Вариант А: k3s на Linux-сервере (по умолчанию)

```bash
# Административный kubeconfig доступен только root: он даёт полный доступ к кластеру.
curl -sfL https://get.k3s.io | sh -s - --write-kubeconfig-mode 600
sudo k3s kubectl get nodes                         # STATUS должен быть Ready
docker --version                                  # нужен для сборки образа
```

Для работы доверенного оператора без sudo скопируйте kubeconfig в его `~/.kube/config`
с владельцем этого оператора и режимом 0600. Это тоже административный доступ:
не предоставляйте такой файл всем пользователям сервера. Для ограниченного доступа
создайте отдельные учётные данные и роли RBAC. Скрипту нужен `kubectl` с доступным
защищённым kubeconfig; при работе от root: `export KUBECONFIG=/etc/rancher/k3s/k3s.yaml`.

Скрипт запускайте **на самом узле k3s**: образ импортируется в его containerd через
`sudo k3s ctr images import` (спросит пароль sudo, если вы не root).

> На VPS, где уже работает compose-стенд, порты 80/443 заняты общим Caddy. Встроенный в k3s
> Traefik тогда не сможет их занять (под `svclb-traefik-…` останется в Pending), но сам кабинет
> поднимется — проверяйте его через `port-forward` (шаг 4). Как совместить Caddy и Traefik —
> отдельное решение, в #25 его не принимали.

### Вариант Б: minikube на ноутбуке (macOS / Linux / Windows)

```bash
# macOS: brew install minikube kubectl   (Docker Desktop или colima — для драйвера docker)
minikube start --memory 4096 --cpus 2
minikube addons enable ingress
kubectl get nodes                                 # minikube   Ready
```

## Шаг 2. Файл .env

```bash
cp .env.example .env
```

Обязательно заполнить:

- `POSTGRES_PASSWORD` — пароль базы в кластере: латиница, цифры и `. _ ~ -` (подставляется в адрес базы);
- `DEMO_PASSWORD` — пароль тестовых учёток (`student1`, `student2`, `dean`);
- `FAKE_NAME_SALT` — любое слово (соль вымышленных ФИО).

По желанию: `TELEGRAM_BOT_TOKEN` (без него бот не запускается), ключи Pyrus, `LLM_PROVIDER` и
`GIGACHAT_AUTH_KEY`. `DATABASE_URL`, `PUBLIC_BASE_URL`, `COOKIE_SECURE`, `RAG_CORPUS_DIR` для
кластера из `.env` **не берутся** — их задаёт скрипт (список `managed` в `deploy/k8s/env-keys.txt`).

`.env` в `.gitignore`, в репозиторий не попадает. В кластере секреты из него лежат в Secret
`lms-secrets`, несекретные настройки — в ConfigMap `lms-env`. Список **закрыт по умолчанию**: в ConfigMap
(открытым текстом) уходят только ключи, помеченные `config`/`envonly` в `deploy/k8s/env-keys.txt`;
любой другой ключ (например, ваш новый `*_TOKEN`) скрипт кладёт в Secret и печатает
«ключ X не описан в env-keys.txt, положен в Secret». Новую несекретную настройку допишите туда как `config`.
Временные файлы с секретами скрипт создаёт вне репозитория и удаляет сразу после применения,
в контекст `docker build` они не попадают.

## Шаг 3. Запуск — одна команда

```bash
sh scripts/k8s-up.sh
```

Скрипт сам поймёт, k3s это или minikube (по контексту kubectl). Первый запуск — несколько минут:
сборка образа, скачивание `postgres:16-alpine` и `redis:7-alpine`, миграции. В конце он печатает адрес.

Варианты:

```bash
K8S_HOST=lms.example.ru sh scripts/k8s-up.sh            # свой домен вместо lms.local
K8S_PUBLIC_URL=http://localhost:8000 sh scripts/k8s-up.sh  # открываете через port-forward (см. шаг 4)
K8S_CLUSTER=minikube sh scripts/k8s-up.sh               # если автоопределение ошиблось
K8S_OVERLAY=external-db sh scripts/k8s-up.sh            # база вне кластера (DATABASE_URL из .env)
K8S_ENV_FILE=deploy/k8s/secrets.env sh scripts/k8s-up.sh  # отдельный файл вместо .env
K8S_SKIP_BUILD=1 K8S_IMAGE_TAG=<тег> sh scripts/k8s-up.sh  # образ уже импортирован
```

Для внешней базы `DATABASE_URL` должен указывать на хост, доступный **из пода**: `localhost`
внутри пода — это сам под, скрипт такой адрес отвергает. Для minikube с внешней базой
замените `traefik` на `nginx` в `deploy/k8s/overlays/external-db/ingress-class.yaml`.

## Шаг 4. Проверка

```bash
kubectl -n mgimo-lms get pods
# postgres-0  1/1 Running;  web-…  1/1 Running;  redis-…  1/1 Running;  bot-…  1/1 Running (если есть токен)

kubectl -n mgimo-lms port-forward svc/web 8000:8000     # в отдельном окне
curl http://localhost:8000/healthz                       # {"ok":true}

kubectl -n mgimo-lms exec deploy/web -c web -- python -m app.seed   # тестовые учётки, один раз
```

Через браузер: http://localhost:8000 (port-forward) или по домену Ingress.

> **Адрес для ссылок.** Ссылка-подписка на календарь (`/feed/<токен>.ics`) и другие абсолютные
> ссылки собираются из `PUBLIC_BASE_URL`. Скрипт ставит его в `http://<K8S_HOST>` (по умолчанию
> `http://lms.local`) — при входе через port-forward такие ссылки не откроются. Если кабинет
> смотрят через `kubectl port-forward svc/web 8000:8000`, перезапустите скрипт с адресом, по которому
> его открывают: `K8S_PUBLIC_URL=http://localhost:8000 sh scripts/k8s-up.sh` (порт — левая часть
> `8000:8000`). В `.env` `PUBLIC_BASE_URL` менять бесполезно: для кластера его задаёт скрипт.
> `COOKIE_SECURE` скрипт выставляет по схеме этого адреса (`https` → true).

Чтобы открылся Чтобы открылся
`http://lms.local`, добавьте в `/etc/hosts` строку `<IP> lms.local`, где IP — из
`kubectl -n mgimo-lms get ingress web` (колонка ADDRESS). minikube на macOS с драйвером docker:
в отдельном окне `minikube tunnel`, IP — `127.0.0.1`.

Логи: `kubectl -n mgimo-lms logs deploy/web -c web -f`, миграции — `… logs deploy/web -c migrate`,
бот — `… logs deploy/bot -f`.

## Обновить кабинет

```bash
git pull
sh scripts/k8s-up.sh
```

Тот же скрипт: новый коммит → новый тег образа → web и bot пересоздаются, миграции применяются
initContainer-ом. Изменили только `.env` — скрипт обновит Secret/ConfigMap и перезапустит web и bot.
Ничего не меняли — повторный запуск ничего не перезапускает. web обновляется стратегией Recreate:
несколько секунд кабинет недоступен (как при `docker compose up -d`).

Несекретные настройки по умолчанию — в `deploy/k8s/base/config.env`; значение из `.env`
их перекрывает.

## Остановить и удалить

```bash
sh scripts/k8s-down.sh          # остановить всё; база, корпус и секреты СОХРАНЯЮТСЯ
sh scripts/k8s-up.sh            # поднять снова с теми же данными
sh scripts/k8s-down.sh --purge  # удалить namespace ЦЕЛИКОМ, вместе с базой (спросит подтверждение)
```

## Данные: корпус ассистента и сертификат GigaChat

Корпус положений (`chunks.jsonl`, PDF) лежит на томе `lms-corpus`, смонтированном в web как
`/data/regulations`. Перенести с VPS:

```bash
POD=$(kubectl -n mgimo-lms get pod -l app.kubernetes.io/name=web -o jsonpath='{.items[0].metadata.name}')
kubectl -n mgimo-lms cp ./data/regulations/chunks.jsonl "$POD":/data/regulations/chunks.jsonl -c web
kubectl -n mgimo-lms rollout restart deployment/web
```

Сертификат Минцифры для GigaChat — необязательный Secret, монтируется в `/data/ca`:

```bash
kubectl -n mgimo-lms create secret generic lms-gigachat-ca --from-file=russian_trusted_root_ca.pem=<путь к файлу>
# в .env: GIGACHAT_CA_BUNDLE=/data/ca/russian_trusted_root_ca.pem, затем sh scripts/k8s-up.sh
```

## HTTPS

По умолчанию Ingress — по http, и `COOKIE_SECURE=false` (иначе браузер не сохранит cookie
входа). Для HTTPS положите сертификат в Secret типа tls и передайте его имя:

```bash
kubectl -n mgimo-lms create secret tls lms-tls --cert=fullchain.pem --key=privkey.pem
K8S_HOST=lms.example.ru K8S_TLS_SECRET=lms-tls sh scripts/k8s-up.sh
```

Скрипт добавит блок `tls` в Ingress, `PUBLIC_BASE_URL=https://…` и `COOKIE_SECURE=true`.
Автоматический выпуск сертификатов (cert-manager) в #25 не входил. Файлы `*.pem`, `*.key`,
`*.crt` в `deploy/k8s/` закрыты `.gitignore`.

## Частые ошибки

| Симптом | Причина и что делать |
|---------|----------------------|
| `kubectl не видит кластер` | k3s: проверить доступ через `sudo k3s kubectl get nodes`; root может задать `KUBECONFIG=/etc/rancher/k3s/k3s.yaml`. Сохранить режим kubeconfig 0600. minikube: `minikube start` |
| Под web в `ErrImageNeverPull` | Образ не попал в кластер. k3s: запускать скрипт на узле k3s; minikube: проверить `minikube image ls \| grep mgimo-lms` |
| web в `Init:Error` / `Init:CrashLoopBackOff` | Не прошли миграции: `kubectl -n mgimo-lms logs deploy/web -c migrate`. Чаще всего база ещё не поднялась или неверный пароль |
| `password authentication failed` после смены `POSTGRES_PASSWORD` | Пароль применяется только при первой инициализации тома. Верните старый пароль или (данные пропадут) `k8s-down.sh --purge` |
| web в `CreateContainerConfigError` | Нет Secret `lms-secrets` — запускайте через `scripts/k8s-up.sh`, а не голым `kubectl apply -k` |
| Вход в кабинет «не держится» | Открываете по http при `COOKIE_SECURE=true`. Скрипт ставит false для http-адреса; не меняйте его в overlay |
| Ссылка-подписка на календарь не открывается | `PUBLIC_BASE_URL` указывает на `lms.local`, а вы ходите через port-forward: `K8S_PUBLIC_URL=http://localhost:8000 sh scripts/k8s-up.sh` |
| `ВНИМАНИЕ: ключ X не описан в env-keys.txt` | Ключ положен в Secret (безопасно). Если он несекретный — допишите `config X` в `deploy/k8s/env-keys.txt` |
| bot в `Init:0/1` долго | Ждёт миграции от web — смотрите логи `migrate`. Без токена бот выключен (0 подов) — это нормально |
| `Read-only file system` в логах web/bot | Библиотека пишет вне `/tmp` и `/data/regulations`. Сообщите в задачу; временно — `readOnlyRootFilesystem: false` в `base/web.yaml` |
| Загрузка PDF в minikube — `413` | Аннотация лимита тела ingress-nginx (`overlays/minikube/ingress-nginx.yaml`, 60m) не применилась |
| `lms.local` не открывается | Нет строки в `/etc/hosts` или (minikube на macOS) не запущен `minikube tunnel`. Проверьте через port-forward |
| postgres-0 в `CrashLoopBackOff`, `permission denied` | Провайдер томов создал каталог без права записи для uid 70. `kubectl -n mgimo-lms logs postgres-0`; для local-path/standard этого быть не должно |

## Чек-лист первого запуска

Для капитана на сервере (k3s). Каждая строка — команда и что должно получиться.

1. `python3 scripts/k8s-validate.py` (нужен PyYAML: `pip install pyyaml`) → `deploy/k8s: OK (…)`.
2. `kubectl get nodes` → узел `Ready`.
3. `kubectl kustomize deploy/k8s/overlays/k3s | head -40` → печатается YAML без ошибок
   (настоящий kustomize; валидатор использует упрощённый).
4. `sh scripts/k8s-up.sh` → в конце `==> кабинет: http://lms.local`, код выхода 0.
   Предупреждений «ключ … не описан в env-keys.txt» быть не должно (иначе — дописать ключ в список).
4а. `kubectl kustomize deploy/k8s/.build | grep -E '^kind:|namespace:' | sort | uniq -c` → у всех
   объектов `namespace: mgimo-lms`, ровно один `kind: ConfigMap` с именем `lms-config-…`, в нём
   `PUBLIC_BASE_URL` и `COOKIE_SECURE` из скрипта (проверка временной kustomization с merge).
5. `kubectl -n mgimo-lms get pods` → `postgres-0`, `web-…`, `redis-…` — `Running`, READY `1/1`;
   `bot-…` — `Running`, если задан токен.
6. `kubectl -n mgimo-lms logs deploy/web -c migrate` → alembic дошёл до последней ревизии
   (`Running upgrade … -> …` при первом запуске), без Traceback.
7. `kubectl -n mgimo-lms port-forward svc/web 8000:8000` и `curl localhost:8000/healthz` → `{"ok":true}`.
8. `kubectl -n mgimo-lms exec deploy/web -c web -- python -m app.seed` → учётки созданы;
   вход `student1` через port-forward в браузере, расписание открывается, ширина 360 px.
9. `kubectl -n mgimo-lms exec deploy/web -c web -- sh -c 'touch /tmp/x /data/regulations/x && rm /tmp/x /data/regulations/x && echo rw-ok'`
   → `rw-ok` (тома записываемы при корне только для чтения).
10. `kubectl -n mgimo-lms exec deploy/web -c web -- python -c "import socket; socket.create_connection(('redis', 6379), 3); print('redis-ok')"` → `redis-ok`.
11. Повторный `sh scripts/k8s-up.sh` без изменений (код закоммичен, `.env` тот же) → `kubectl -n mgimo-lms get pods` показывает те же
    поды (AGE не сбросился) — скрипт идемпотентен.
12. `sh scripts/k8s-down.sh`, затем `sh scripts/k8s-up.sh` → учётка `student1` по-прежнему входит
    (данные пережили остановку).
13. Записать результат (что прошло, что нет) в `docs/prompts/infra.md`.

## Что проверено без кластера

- `scripts/k8s-validate.py` (тест `tests/test_k8s_manifests.py`, образец workflow `deploy/k8s/ci/k8s-validate.yml.example` — чтобы включить проверку в GitHub Actions, скопируйте его в `.github/workflows/k8s-validate.yml` (нужен доступ с правом `workflow`)): разбор всех YAML; сборка трёх overlay упрощённым
  kustomize; ссылки на файлы; селекторы и порты Service, Ingress, NetworkPolicy; PVC,
  ConfigMap, Secret; пробы, ресурсы, securityContext; число реплик и стратегия web/bot;
  отсутствие Secret в репозитории; каждая переменная `.env.example` попадает в ConfigMap или Secret,
  имена совпадают с `app/core/config.py`; строгие JSON-схемы Kubernetes 1.29 и 1.33
  (pip-пакет `kubernetes-validate`); `sh -n` для скриптов.
- `scripts/k8s-up.sh` и `k8s-down.sh` прогнаны с заглушками kubectl/k3s/docker: разбор `.env`
  (комментарии, кавычки, CRLF, `export`, пустые значения), отказ при опасном пароле и
  `localhost` во внешней базе, повторный запуск без пересборки, TLS, `--purge` с подтверждением.
- **Не проверено:** настоящий `kustomize build`, запуск подов, миграции в кластере, Traefik/
  ingress-nginx, права томов, работа бота и ассистента в кластере.
