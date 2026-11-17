#!/bin/sh
# Готовность standby означает работающую потоковую репликацию, не только живой процесс.
set -eu
test "$(psql -X -v ON_ERROR_STOP=1 -At -U lms -d lms -c \
    "SELECT pg_is_in_recovery() AND EXISTS (SELECT FROM pg_stat_wal_receiver WHERE status='streaming')")" = t
