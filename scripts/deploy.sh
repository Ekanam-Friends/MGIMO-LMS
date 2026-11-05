#!/bin/sh
# Выкладка закоммиченного HEAD на сервер (репозиторий приватный, сервер его не клонирует).
#   sh scripts/deploy.sh
# .env на сервере не трогается: он создаётся один раз руками (см. docs/deploy.md).
set -eu
HOST=${DEPLOY_HOST:-root@195.24.71.184}
DIR=${DEPLOY_DIR:-/opt/mgimo-lms}
git archive --format=tar HEAD | ssh "$HOST" "mkdir -p $DIR && tar -x -C $DIR && cd $DIR && docker compose up -d --build web && docker compose ps"
