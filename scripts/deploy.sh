#!/bin/sh
# Выкладка закоммиченного HEAD на сервер (репозиторий приватный, сервер его не клонирует).
#   sh scripts/deploy.sh
# .env на сервере не трогается: он создаётся один раз руками (см. docs/deploy.md).
# Бот (решение 0005) поднимается, только если в серверном .env задан TELEGRAM_BOT_TOKEN.
set -eu
HOST=${DEPLOY_HOST:-root@195.24.71.184}
DIR=${DEPLOY_DIR:-/opt/mgimo-lms}
git archive --format=tar HEAD | ssh "$HOST" "mkdir -p $DIR && tar -x -C $DIR && cd $DIR \
  && docker compose up -d --build web \
  && if grep -q '^TELEGRAM_BOT_TOKEN=.' .env; then docker compose up -d --build bot; else echo 'TELEGRAM_BOT_TOKEN не задан — бот не запущен'; fi \
  && docker compose ps"
