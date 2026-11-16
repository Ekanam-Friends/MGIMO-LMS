#!/bin/sh
# Первый старт копирует кластер; существующий том никогда не очищается автоматически.
set -eu
if [ ! -s "$PGDATA/PG_VERSION" ]; then
    if [ -n "$(ls -A "$PGDATA")" ]; then
        echo 'Непустой незавершённый том: требуется ручная проверка, очистка запрещена.' >&2
        exit 1
    fi
    mkdir -p "$PGDATA"
    chmod 700 "$PGDATA"
    chown postgres:postgres "$PGDATA"
    # Пароль находится только в секретном файле и .pgpass с правами 0600.
    umask 077
    printf 'db:5432:replication:lms_replica:%s\n' "$(cat /run/secrets/replication_password)" > /tmp/replica.pgpass
    chown postgres:postgres /tmp/replica.pgpass
    gosu postgres env PGPASSFILE=/tmp/replica.pgpass pg_basebackup \
        --dbname='host=db port=5432 user=lms_replica application_name=mgimo_lms_replica' \
        --pgdata="$PGDATA" --wal-method=stream --slot=mgimo_lms_replica --write-recovery-conf \
        --checkpoint=fast --no-password
    mv /tmp/replica.pgpass "$PGDATA/.pgpass"
    # -R записывает временный passfile; используем постоянный внутри тома.
    sed -i "s|/tmp/replica.pgpass|$PGDATA/.pgpass|g" "$PGDATA/postgresql.auto.conf"
fi
if [ ! -f "$PGDATA/standby.signal" ]; then
    echo 'Том уже promoted либо не является standby. Автоматический старт запрещён.' >&2
    exit 1
fi
exec /usr/local/bin/docker-entrypoint.sh "$@"
