"""Настройка standby на VPS из корня /opt/mgimo-lms; Python 3, Docker Compose."""
import datetime
import json
import os
from pathlib import Path
import secrets
import subprocess


ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml"]
if (ROOT / "docker-compose.override.yml").exists():
    COMPOSE += ["-f", "docker-compose.override.yml"]
COMPOSE += ["-f", "docker-compose.replica.yml"]


def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, check=True, **kwargs)


def sql(statement, env=None):
    command = COMPOSE + ["exec", "-T"]
    if env is not None:
        command += ["-e", "REPLICATION_PASSWORD"]
    command += ["db", "psql", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-U", "lms", "-d", "lms"]
    return run(command, input=statement, text=True, capture_output=True, env=env).stdout.strip()


def main():
    # Не меняем конфигурацию и роли, если это уже standby или параметры требуют рестарта.
    if sql("SELECT pg_is_in_recovery()") != "f":
        raise SystemExit("db не является primary; требуется ручная проверка")
    for name, valid in [("wal_level", {"replica", "logical"})]:
        if sql(f"SHOW {name}") not in valid:
            raise SystemExit(f"{name} требует изменения с рестартом: остановка не выполняется")
    if int(sql("SHOW max_wal_senders")) < 2 or int(sql("SHOW max_replication_slots")) < 1:
        raise SystemExit("Недостаточно WAL senders/slots; нужен отдельный план рестарта")

    # Бэкап до первой записи; проверяем читаемость архива pg_restore.
    backup_root = ROOT / "backups" / "replica-setup"
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_root / datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup.mkdir(mode=0o700)
    with (backup / "lms.dump").open("wb") as out:
        run(COMPOSE + ["exec", "-T", "db", "pg_dump", "-U", "lms", "-d", "lms", "-Fc"], stdout=out)
    with (backup / "lms.dump").open("rb") as inp:
        run(COMPOSE + ["exec", "-T", "db", "pg_restore", "--list"], stdin=inp, stdout=subprocess.DEVNULL)
    for name in ("pg_hba.conf", "postgresql.auto.conf"):
        with (backup / name).open("wb") as out:
            run(COMPOSE + ["exec", "-T", "db", "cat", f"/var/lib/postgresql/data/{name}"], stdout=out)

    secret_dir = ROOT / ".secrets"
    secret_dir.mkdir(exist_ok=True, mode=0o700)
    secret = secret_dir / "replication-password"
    if not secret.exists():
        if sql("SELECT count(*) FROM pg_roles WHERE rolname='lms_replica'") != "0":
            raise SystemExit("Роль существует, секрет потерян: автоматическая смена пароля запрещена")
        with open(secret, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as out:
            out.write(secrets.token_hex(32))
    password = secret.read_text().strip()
    if len(password) != 64 or any(c not in "0123456789abcdef" for c in password):
        raise SystemExit("Неожиданный формат секрета")
    env = dict(os.environ, REPLICATION_PASSWORD=password)
    sql(r"""\getenv replica_password REPLICATION_PASSWORD
SELECT 'CREATE ROLE lms_replica LOGIN REPLICATION' WHERE NOT EXISTS
 (SELECT FROM pg_roles WHERE rolname='lms_replica') \gexec
SELECT format('ALTER ROLE lms_replica WITH LOGIN REPLICATION NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', :'replica_password') \gexec
""", env=env)
    # HBA ограничен подсетью default-сети проекта; чужая сеть Ikanam не используется.
    cid = run(COMPOSE + ["ps", "-q", "db"], capture_output=True, text=True).stdout.strip()
    container = json.loads(run(["docker", "inspect", cid], capture_output=True, text=True).stdout)[0]
    network_name = next(n for n in container["NetworkSettings"]["Networks"] if n.endswith("_default"))
    network = json.loads(run(["docker", "network", "inspect", network_name], capture_output=True, text=True).stdout)[0]
    subnet = network["IPAM"]["Config"][0]["Subnet"]
    rule = f"host replication lms_replica {subnet} scram-sha-256"
    hba = run(COMPOSE + ["exec", "-T", "db", "cat", "/var/lib/postgresql/data/pg_hba.conf"], capture_output=True, text=True).stdout
    if rule not in hba.splitlines():
        run(COMPOSE + ["exec", "-T", "db", "sh", "-c", 'cat >> "$PGDATA/pg_hba.conf"'], input="\n" + rule + "\n", text=True)
    sql("ALTER SYSTEM SET max_slot_wal_keep_size = '512MB'; SELECT pg_reload_conf();")
    if sql("SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL") != "0":
        raise SystemExit("Ошибка в HBA: проверьте конфигурацию и бэкап")
    sql("SELECT pg_create_physical_replication_slot('mgimo_lms_replica') WHERE NOT EXISTS "
        "(SELECT FROM pg_replication_slots WHERE slot_name='mgimo_lms_replica');")
    run(COMPOSE + ["up", "-d", "--no-deps", "--wait", "--wait-timeout", "180", "db-replica"])
    # Чистим только свои проверенные копии; максимум три на файл.
    import shutil
    for old in sorted(backup_root.iterdir())[:-3]:
        if old.is_dir() and not old.is_symlink() and old.resolve().parent == backup_root.resolve():
            shutil.rmtree(old)
    print("Реплика запущена. Проверка: sh scripts/server/check-replica.sh")


if __name__ == "__main__":
    main()
