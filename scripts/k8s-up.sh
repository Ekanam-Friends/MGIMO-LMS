#!/bin/sh
# Поднять кабинет в Kubernetes (k3s или minikube) одной командой. Решение 0028,
# инструкция — docs/kubernetes.md. VPS-стенд остаётся на docker compose (scripts/deploy.sh).
#
#   sh scripts/k8s-up.sh
#
# Что делает (повторный запуск безопасен — ничего не дублирует):
#   1. проверяет kubectl, доступ к кластеру и файл .env;
#   2. раскладывает .env по deploy/k8s/env-keys.txt: в ConfigMap lms-env — только ключи,
#      явно помеченные config/envonly; всё остальное, в том числе неописанные ключи, —
#      в Secret lms-secrets (закрыто по умолчанию);
#   3. собирает образ из Dockerfile и загружает его в кластер
#      (k3s: k3s ctr images import, minikube: minikube image load);
#   4. kubectl apply -k для выбранного overlay с тегом образа и хостом;
#   5. ждёт, пока поднимутся postgres, redis, web, bot, и печатает адрес.
#
# Переменные (все необязательные):
#   K8S_CLUSTER     k3s | minikube — куда грузить образ (по умолчанию: по текущему контексту kubectl)
#   K8S_OVERLAY     k3s | minikube | external-db (по умолчанию = K8S_CLUSTER)
#   K8S_ENV_FILE    файл с настройками и секретами (по умолчанию .env)
#   K8S_HOST        хост в Ingress (по умолчанию lms.local)
#   K8S_PUBLIC_URL  внешний адрес кабинета для ссылок (PUBLIC_BASE_URL), если вы открываете его
#                   не по K8S_HOST — например http://localhost:8000 при kubectl port-forward
#   K8S_TLS_SECRET  имя Secret типа kubernetes.io/tls в namespace mgimo-lms -> включает HTTPS
#   K8S_IMAGE_TAG   тег образа (по умолчанию — коммит git; с любыми незакоммиченными
#                   или неотслеживаемыми файлами — с меткой времени, образ пересобирается)
#   K8S_SKIP_BUILD  1 — не собирать и не загружать образ (он уже есть в кластере с этим тегом)
#   K8S_TIMEOUT     сколько ждать выкатку, по умолчанию 600s
set -eu

cd "$(dirname "$0")/.."
ROOT=$(pwd)
NS=mgimo-lms
K8S_DIR=deploy/k8s
BUILD_DIR=$K8S_DIR/.build
KEYS_FILE=$K8S_DIR/env-keys.txt
IMAGE=mgimo-lms

ENV_FILE=${K8S_ENV_FILE:-.env}
HOST=${K8S_HOST:-lms.local}
TLS_SECRET=${K8S_TLS_SECRET:-}
PUBLIC_URL_OVERRIDE=${K8S_PUBLIC_URL:-}
TIMEOUT=${K8S_TIMEOUT:-600s}
SKIP_BUILD=${K8S_SKIP_BUILD:-0}

say() { printf '==> %s\n' "$*"; }
warn() { printf 'ВНИМАНИЕ: %s\n' "$*" >&2; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# --- 1. Проверки -----------------------------------------------------------------------------
have kubectl || die "не найден kubectl. Установка — docs/kubernetes.md, шаг 1"
[ -f "$ENV_FILE" ] || die "нет файла $ENV_FILE. Скопируйте .env.example в .env и заполните (docs/kubernetes.md, шаг 2)"
[ -f "$KEYS_FILE" ] || die "нет $KEYS_FILE — запускайте из репозитория MGIMO-LMS"
kubectl cluster-info --request-timeout=15s >/dev/null 2>&1 \
  || die "kubectl не видит кластер. Проверьте: kubectl get nodes (для k3s — KUBECONFIG, docs/kubernetes.md)"

CONTEXT=$(kubectl config current-context 2>/dev/null || echo "")
if [ -n "${K8S_CLUSTER:-}" ]; then
  CLUSTER=$K8S_CLUSTER
elif [ "$CONTEXT" = minikube ]; then
  CLUSTER=minikube
elif have k3s; then
  CLUSTER=k3s
else
  die "не понял, какой это кластер (контекст '$CONTEXT'). Задайте K8S_CLUSTER=k3s или K8S_CLUSTER=minikube"
fi
case "$CLUSTER" in k3s|minikube) ;; *) die "K8S_CLUSTER должен быть k3s или minikube, а не '$CLUSTER'" ;; esac
OVERLAY=${K8S_OVERLAY:-$CLUSTER}
[ -f "$K8S_DIR/overlays/$OVERLAY/kustomization.yaml" ] || die "нет overlay '$OVERLAY' (есть: k3s, minikube, external-db)"
EXTERNAL_DB=0
[ "$OVERLAY" = external-db ] && EXTERNAL_DB=1

case "$HOST" in
  *[!a-z0-9.-]*|''|.*|-*) die "K8S_HOST='$HOST': допустимы строчные латинские буквы, цифры, точка и дефис" ;;
esac
case "$TLS_SECRET" in
  *[!a-z0-9.-]*) die "K8S_TLS_SECRET='$TLS_SECRET': имя Secret — строчные буквы, цифры, точка, дефис" ;;
esac
case "$PUBLIC_URL_OVERRIDE" in
  ''|http://*|https://*) ;;
  *) die "K8S_PUBLIC_URL='$PUBLIC_URL_OVERRIDE' должен начинаться с http:// или https://" ;;
esac
case "$PUBLIC_URL_OVERRIDE" in
  *[!A-Za-z0-9.:/_-]*) die "K8S_PUBLIC_URL: допустимы латиница, цифры и . : / _ -" ;;
esac
say "кластер: $CLUSTER (контекст '$CONTEXT'), overlay: $OVERLAY, хост: $HOST"

# --- 2. Раскладываем .env на секреты и настройки ---------------------------------------------
mkdir -p "$BUILD_DIR"
umask 077
# Файлы с секретами — во временном каталоге ВНЕ репозитория: они не попадут ни в контекст
# docker build, ни в VM при minikube image build, ни в git.
SECRET_DIR=$(mktemp -d "${TMPDIR:-/tmp}/mgimo-lms-k8s.XXXXXX")
SECRET_ENV=$SECRET_DIR/secrets.env
PLAIN_ENV=$SECRET_DIR/lms-env.env
cleanup() { rm -rf "$SECRET_DIR"; }
trap cleanup EXIT
trap 'exit 1' INT TERM
: > "$SECRET_ENV"
: > "$PLAIN_ENV"

in_list() { # in_list <secret|managed|config|envonly> <KEY>
  awk -v kind="$1" -v key="$2" '{ sub(/\r$/, "") } $1 == kind && $2 == key { found = 1 } END { exit !found }' "$KEYS_FILE"
}

POSTGRES_PASSWORD_VALUE=
DATABASE_URL_VALUE=
TELEGRAM_TOKEN_SET=0
# Как docker compose: KEY=VALUE, комментарии и пустые строки пропускаются, кавычки вокруг
# значения снимаются, CR в конце строки (файл из Windows) убирается.
while IFS= read -r line || [ -n "$line" ]; do
  line=$(printf '%s' "$line" | tr -d '\r')
  case "$line" in ''|'#'*) continue ;; esac
  line=${line#export }
  case "$line" in *=*) ;; *) continue ;; esac
  key=${line%%=*}
  value=${line#*=}
  case "$key" in ''|*[!A-Za-z0-9_]*) warn "строка с ключом '$key' в $ENV_FILE пропущена"; continue ;; esac
  case "$value" in
    \"*\") value=${value#\"}; value=${value%\"} ;;
    \'*\') value=${value#\'}; value=${value%\'} ;;
  esac
  # Пустое значение не передаём: иначе, например, PYRUS_FORM_ID= не парсится как число.
  [ -n "$value" ] || continue
  case "$key" in
    POSTGRES_PASSWORD) POSTGRES_PASSWORD_VALUE=$value ;;
    DATABASE_URL) DATABASE_URL_VALUE=$value; continue ;;
    TELEGRAM_BOT_TOKEN) TELEGRAM_TOKEN_SET=1 ;;
  esac
  # Закрыто по умолчанию: в ConfigMap (открытый текст) — только явно несекретные ключи.
  if in_list managed "$key"; then
    continue
  elif in_list config "$key" || in_list envonly "$key"; then
    printf '%s=%s\n' "$key" "$value" >> "$PLAIN_ENV"
  else
    if ! in_list secret "$key"; then
      warn "ключ $key не описан в $KEYS_FILE — положен в Secret"
    fi
    printf '%s=%s\n' "$key" "$value" >> "$SECRET_ENV"
  fi
done < "$ENV_FILE"

if [ "$EXTERNAL_DB" = 1 ]; then
  [ -n "$DATABASE_URL_VALUE" ] || die "для external-db задайте DATABASE_URL в $ENV_FILE"
  case "$DATABASE_URL_VALUE" in
    *@localhost*|*@127.0.0.1*|*@db:*|*@db/*)
      die "DATABASE_URL указывает на '$(printf '%s' "$DATABASE_URL_VALUE" | sed 's|.*@||')': из пода это не внешняя база. Нужен адрес, доступный из кластера" ;;
  esac
  printf 'DATABASE_URL=%s\n' "$DATABASE_URL_VALUE" >> "$SECRET_ENV"
else
  [ -n "$POSTGRES_PASSWORD_VALUE" ] || die "задайте POSTGRES_PASSWORD в $ENV_FILE (пароль базы в кластере)"
  case "$POSTGRES_PASSWORD_VALUE" in
    *[!A-Za-z0-9._~-]*) die "POSTGRES_PASSWORD содержит символы, которые ломают DATABASE_URL. Используйте латиницу, цифры и . _ ~ -" ;;
  esac
  # Значение DATABASE_URL из .env (обычно localhost для запуска без кластера) не используется.
  printf 'DATABASE_URL=postgresql+asyncpg://lms:%s@postgres:5432/lms\n' "$POSTGRES_PASSWORD_VALUE" >> "$SECRET_ENV"
fi
grep -q '^DEMO_PASSWORD=' "$SECRET_ENV" || warn "DEMO_PASSWORD пуст — python -m app.seed не создаст тестовые учётки"
# ConfigMap не бывает пустым у kubectl create --from-env-file; служебный ключ приложение игнорирует.
printf 'K8S_ENV_SOURCE=%s\n' "$(basename "$ENV_FILE")" >> "$PLAIN_ENV"

BOT_REPLICAS=1
if [ "$TELEGRAM_TOKEN_SET" = 0 ]; then
  BOT_REPLICAS=0
  warn "TELEGRAM_BOT_TOKEN пуст — бот не запускается (replicas: 0), как и в compose"
fi

if [ -n "$TLS_SECRET" ]; then SCHEME=https; COOKIE_SECURE=true; else SCHEME=http; COOKIE_SECURE=false; fi
PUBLIC_URL=${PUBLIC_URL_OVERRIDE:-$SCHEME://$HOST}
# Cookie Secure — по фактической схеме адреса: по http браузер такую cookie не сохранит.
case "$PUBLIC_URL" in https://*) COOKIE_SECURE=true ;; *) COOKIE_SECURE=false ;; esac

# --- 3. Образ ---------------------------------------------------------------------------------
if [ -n "${K8S_IMAGE_TAG:-}" ]; then
  TAG=$K8S_IMAGE_TAG
elif have git && git rev-parse --git-dir >/dev/null 2>&1; then
  TAG=$(git rev-parse --short=12 HEAD)
  # Неотслеживаемые файлы тоже считаются: новый модуль в app/ попадает в docker build,
  # и образ с тегом «чистого» коммита был бы уже другим.
  if [ -n "$(git status --porcelain)" ]; then
    TAG=$TAG-dirty-$(date +%Y%m%d%H%M%S)
  fi
else
  TAG=local-$(date +%Y%m%d%H%M%S)
fi
case "$TAG" in *[!A-Za-z0-9_.-]*|'') die "K8S_IMAGE_TAG='$TAG': допустимы буквы, цифры, _ . -" ;; esac
REF=$IMAGE:$TAG

SUDO=
if [ "$(id -u)" != 0 ] && have sudo; then SUDO=sudo; fi

build_docker() {
  say "сборка образа $REF (docker build)"
  docker build -t "$REF" "$ROOT"
}

if [ "$SKIP_BUILD" = 1 ]; then
  say "K8S_SKIP_BUILD=1: образ $REF не собирается — он должен уже быть в кластере"
elif [ "$CLUSTER" = minikube ]; then
  have minikube || die "не найден minikube"
  if have docker; then
    build_docker
    say "загрузка образа в minikube"
    minikube image load "$REF"
  else
    say "docker не найден — собираю внутри minikube (minikube image build)"
    minikube image build -t "$REF" "$ROOT"
  fi
else
  have k3s || die "не найден k3s. Скрипт нужно запускать на узле k3s (образ импортируется в его containerd)"
  if [ "${TAG#*-dirty-}" = "$TAG" ] && $SUDO k3s ctr images ls -q 2>/dev/null | grep -qx "docker.io/library/$REF"; then
    say "образ $REF уже есть в k3s — сборка пропущена"
  else
    # Только docker: nerdctl на голом k3s не видит его containerd и без buildkitd не собирает.
    have docker || die "нужен docker, чтобы собрать образ (docs/kubernetes.md, шаг 1). Или соберите образ в другом месте, импортируйте и запустите с K8S_SKIP_BUILD=1"
    build_docker
    say "импорт образа в k3s (k3s ctr images import)"
    docker save "$REF" | $SUDO k3s ctr images import -
    $SUDO k3s ctr images ls -q | grep -qx "docker.io/library/$REF" \
      || die "образ $REF не появился в k3s после импорта (см. вывод выше)"
  fi
fi

# --- 4. Применяем манифесты -------------------------------------------------------------------
say "namespace, Secret lms-secrets и ConfigMap lms-env"
kubectl apply -f "$K8S_DIR/base/namespace.yaml"
kubectl -n "$NS" create secret generic lms-secrets --from-env-file="$SECRET_ENV" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n "$NS" create configmap lms-env --from-env-file="$PLAIN_ENV" \
  --dry-run=client -o yaml | kubectl apply -f -

# Контрольная сумма .env в шаблоне пода: изменили секрет — web и bot перезапустятся,
# ничего не меняли — повторный запуск ничего не перезапускает.
CHECKSUM=$(cat "$SECRET_ENV" "$PLAIN_ENV" | cksum | awk '{print $1}')

{
  echo "# Создано scripts/k8s-up.sh — не редактировать и не коммитить (.gitignore)."
  echo "apiVersion: kustomize.config.k8s.io/v1beta1"
  echo "kind: Kustomization"
  echo "namespace: $NS"
  echo "resources:"
  echo "  - ../overlays/$OVERLAY"
  echo "images:"
  echo "  - name: $IMAGE"
  echo "    newTag: \"$TAG\""
  echo "replicas:"
  echo "  - name: bot"
  echo "    count: $BOT_REPLICAS"
  echo "configMapGenerator:"
  echo "  - name: lms-config"
  echo "    behavior: merge"
  echo "    literals:"
  echo "      - PUBLIC_BASE_URL=$PUBLIC_URL"
  echo "      - COOKIE_SECURE=$COOKIE_SECURE"
  echo "patches:"
  echo "  - target: {kind: Ingress, name: web}"
  echo "    patch: |-"
  echo "      - op: replace"
  echo "        path: /spec/rules/0/host"
  echo "        value: $HOST"
  if [ -n "$TLS_SECRET" ]; then
    echo "      - op: add"
    echo "        path: /spec/tls"
    echo "        value: [{hosts: [\"$HOST\"], secretName: \"$TLS_SECRET\"}]"
  fi
  for deployment in web bot; do
    echo "  - target: {kind: Deployment, name: $deployment}"
    echo "    patch: |-"
    echo "      apiVersion: apps/v1"
    echo "      kind: Deployment"
    echo "      metadata: {name: $deployment}"
    echo "      spec: {template: {metadata: {annotations: {mgimo-lms/env-checksum: \"$CHECKSUM\"}}}}"
  done
} > "$BUILD_DIR/kustomization.yaml"

say "kubectl apply -k ($OVERLAY, образ $REF)"
kubectl apply -k "$BUILD_DIR"

# --- 5. Ждём ---------------------------------------------------------------------------------
say "жду выкатку (до $TIMEOUT; первый запуск — скачивание postgres/redis и миграции)"
if [ "$EXTERNAL_DB" = 0 ]; then
  kubectl -n "$NS" rollout status statefulset/postgres --timeout="$TIMEOUT"
fi
kubectl -n "$NS" rollout status deployment/redis --timeout="$TIMEOUT"
if ! kubectl -n "$NS" rollout status deployment/web --timeout="$TIMEOUT"; then
  warn "web не поднялся. Логи миграций: kubectl -n $NS logs deploy/web -c migrate"
  warn "логи web: kubectl -n $NS logs deploy/web -c web; события: kubectl -n $NS get events --sort-by=.lastTimestamp"
  exit 1
fi
if [ "$BOT_REPLICAS" = 1 ]; then
  kubectl -n "$NS" rollout status deployment/bot --timeout="$TIMEOUT"
fi

kubectl -n "$NS" get pods,svc,ingress
ADDRESS=$(kubectl -n "$NS" get ingress web -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || true)
echo
say "кабинет: $PUBLIC_URL"
if [ -n "$ADDRESS" ]; then
  echo "    Если $HOST не резолвится, добавьте в /etc/hosts строку:  $ADDRESS $HOST"
else
  echo "    Адрес Ingress ещё не назначен: kubectl -n $NS get ingress web (колонка ADDRESS)."
fi
if [ "$CLUSTER" = minikube ]; then
  echo "    minikube на macOS/Windows с драйвером docker: в отдельном окне minikube tunnel, адрес 127.0.0.1"
fi
echo "    Проверка без Ingress: kubectl -n $NS port-forward svc/web 8000:8000  ->  http://localhost:8000/healthz"
echo "    Тестовые учётки (один раз): kubectl -n $NS exec deploy/web -c web -- python -m app.seed"
