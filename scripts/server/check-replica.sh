#!/bin/sh
set -eu
# Запускать из корня проекта на сервере; override автоматически подхватится compose.
docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -U lms -d lms -c "
SELECT application_name, state, sync_state,
       pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS lag_bytes,
       write_lag, flush_lag, replay_lag
FROM pg_stat_replication;
SELECT slot_name, active, wal_status,
       pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes
FROM pg_replication_slots;"
docker compose -f docker-compose.yml -f docker-compose.replica.yml exec -T db-replica psql -X -v ON_ERROR_STOP=1 -U lms -d lms -c "
SELECT pg_is_in_recovery(), current_setting('transaction_read_only') AS read_only,
       pg_last_wal_receive_lsn(), pg_last_wal_replay_lsn();"
test "$(docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -At -U lms -d lms -c \
    "SELECT count(*) FROM pg_stat_replication WHERE application_name='mgimo_lms_replica' AND state='streaming'")" = 1
test "$(docker compose -f docker-compose.yml -f docker-compose.replica.yml exec -T db-replica \
    psql -X -v ON_ERROR_STOP=1 -At -U lms -d lms -c "SELECT pg_is_in_recovery() AND current_setting('transaction_read_only')='on'")" = t
test "$(docker compose exec -T db psql -X -v ON_ERROR_STOP=1 -At -U lms -d lms -c \
    "SELECT count(*) FROM pg_replication_slots WHERE slot_name='mgimo_lms_replica' AND active")" = 1
docker compose -f docker-compose.yml -f docker-compose.replica.yml exec -T db-replica \
    sh /usr/local/bin/replica-healthcheck.sh
