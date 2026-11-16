"""Одноразовый контур проверки standby. Запуск: RUN_REPLICA_TESTS=1 pytest -q tests/test_replica_setup.py."""
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_REPLICA_TESTS") != "1", reason="Нужен локальный Docker")
ROOT = Path(__file__).resolve().parents[1]


def test_replica_install_restart_disconnect_and_promotion(tmp_path):
    project = "mgimo-review-pr39-" + uuid.uuid4().hex[:8]
    env = dict(os.environ, COMPOSE_PROJECT_NAME=project, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    for name in ("docker-compose.replica.yml", "docker-compose.failover.yml", "scripts/setup_replica.py",
                 "scripts/server/replica-entrypoint.sh", "scripts/server/replica-healthcheck.sh",
                 "scripts/server/check-replica.sh"):
        dest = tmp_path / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((ROOT / name).read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8"))
    (tmp_path / ".env").write_text("POSTGRES_PASSWORD=review-only-local\n", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("""
services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: lms
      POSTGRES_DB: lms
      POSTGRES_PASSWORD: review-only-local
    volumes:
      - primary-data:/var/lib/postgresql/data
    healthcheck:
      test: [CMD, pg_isready, -U, lms, -d, lms]
      interval: 1s
      retries: 30
  web:
    image: postgres:16-alpine
    command: ["true"]
  bot:
    image: postgres:16-alpine
    command: ["true"]
volumes:
  primary-data:
""", encoding="utf-8")

    def run(args, *, check=True, input=None):
        return subprocess.run(args, cwd=tmp_path, env=env, check=check, input=input,
            text=True, encoding="utf-8", capture_output=True, timeout=240)

    base = ["docker", "compose", "-p", project, "-f", "docker-compose.yml"]
    both = base + ["-f", "docker-compose.replica.yml"]
    failover = both + ["-f", "docker-compose.failover.yml"]

    def sql(service, query, *, check=True):
        return run(both + ["exec", "-T", service, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-At", "-U", "lms", "-d", "lms", "-c", query], check=check)

    def eventually(call, expected):
        deadline = time.monotonic() + 40
        while True:
            result = call()
            if result.returncode == 0 and result.stdout.strip() == expected:
                return
            if time.monotonic() >= deadline:
                pytest.fail(f"Не достигнуто {expected!r}: {result.stdout} {result.stderr}")
            time.sleep(1)

    try:
        run(base + ["up", "-d", "--wait", "db"])
        run([sys.executable, "scripts/setup_replica.py"])
        sql("db", "CREATE TABLE replica_probe (id integer PRIMARY KEY); INSERT INTO replica_probe VALUES (17)")
        eventually(lambda: sql("db-replica", "SELECT id FROM replica_probe", check=False), "17")
        assert sql("db-replica", "INSERT INTO replica_probe VALUES (18)", check=False).returncode != 0
        for _ in range(3):
            run([sys.executable, "scripts/setup_replica.py"])
        assert len(list((tmp_path / "backups/replica-setup").iterdir())) == 3
        run(both + ["restart", "db-replica"])
        eventually(lambda: sql("db-replica", "SELECT pg_is_in_recovery()", check=False), "t")
        run(both + ["stop", "db"])
        # Живой standby без primary не должен проходить проверку готовности.
        deadline = time.monotonic() + 40
        while run(both + ["exec", "-T", "db-replica", "sh", "/usr/local/bin/replica-healthcheck.sh"], check=False).returncode == 0:
            assert time.monotonic() < deadline
            time.sleep(1)
        run(both + ["start", "db"])
        eventually(lambda: sql("db", "SELECT count(*) FROM pg_stat_replication WHERE state='streaming'", check=False), "1")
        run(both + ["stop", "db"])
        assert sql("db-replica", "SELECT pg_promote(true, 30)").stdout.strip() == "t"
        run(failover + ["up", "-d", "--no-deps", "--wait", "db-replica"])
        assert sql("db-replica", "SELECT pg_is_in_recovery()").stdout.strip() == "f"
        sql("db-replica", "INSERT INTO replica_probe VALUES (19)")
        assert sql("db-replica", "SELECT count(*) FROM replica_probe").stdout.strip() == "2"
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"Команда завершилась ошибкой: {exc.cmd}\n{exc.stdout}\n{exc.stderr}")
    finally:
        run(both + ["down", "--volumes", "--remove-orphans"], check=False)
