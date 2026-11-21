#!/bin/sh
# Остановить кабинет в Kubernetes (решение 0028, docs/kubernetes.md).
#
#   sh scripts/k8s-down.sh           — удалить web, bot, redis, postgres, Ingress, Service, ConfigMap.
#                                      ДАННЫЕ ОСТАЮТСЯ: тома базы и корпуса (PVC) и Secret lms-secrets.
#                                      Следующий scripts/k8s-up.sh поднимет кабинет с теми же данными.
#   sh scripts/k8s-down.sh --purge   — удалить namespace mgimo-lms целиком, ВМЕСТЕ С БАЗОЙ.
#                                      Спросит подтверждение; без вопроса: --purge --yes.
#
# Повторный запуск безопасен: то, чего уже нет, просто пропускается.
set -eu

NS=mgimo-lms
PURGE=0
YES=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes) YES=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) printf 'ОШИБКА: неизвестный параметр %s (есть --purge, --yes)\n' "$arg" >&2; exit 1 ;;
  esac
done

command -v kubectl >/dev/null 2>&1 || { echo "ОШИБКА: не найден kubectl" >&2; exit 1; }

if ! kubectl get namespace "$NS" >/dev/null 2>&1; then
  echo "namespace $NS не найден — удалять нечего"
  exit 0
fi

if [ "$PURGE" = 1 ]; then
  if [ "$YES" != 1 ]; then
    printf 'Будет удалён namespace %s вместе с базой PostgreSQL, корпусом и секретами.\n' "$NS"
    printf 'Это необратимо. Введите %s для подтверждения: ' "$NS"
    read -r answer || answer=
    [ "$answer" = "$NS" ] || { echo "отменено"; exit 1; }
  fi
  kubectl delete namespace "$NS" --ignore-not-found --wait=true
  echo "namespace $NS удалён"
  exit 0
fi

# Только объекты с меткой кабинета. PVC, Secret и созданный скриптом ConfigMap lms-env
# в список не входят — данные и настройки сохраняются.
kubectl -n "$NS" delete ingress,deployment,statefulset,service,networkpolicy,configmap \
  -l app.kubernetes.io/part-of=mgimo-lms --ignore-not-found --wait=true
echo "кабинет остановлен; данные сохранены:"
kubectl -n "$NS" get pvc,secret 2>/dev/null || true
echo "Удалить всё вместе с данными: sh scripts/k8s-down.sh --purge"
