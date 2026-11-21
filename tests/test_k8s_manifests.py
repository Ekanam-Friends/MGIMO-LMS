"""Статическая проверка манифестов Kubernetes (deploy/k8s, решение 0028) без живого кластера."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
yaml = pytest.importorskip("yaml", reason="валидатору манифестов нужен PyYAML")


def load_validator():
    spec = importlib.util.spec_from_file_location("k8s_validate", ROOT / "scripts" / "k8s-validate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifests_pass_validator(capsys):
    validator = load_validator()
    assert validator.main([]) == 0, capsys.readouterr().err


def test_minikube_ingress_allows_assistant_deadline():
    """Прокси должен дождаться полного дедлайна ассистента (70 с)."""
    validator = load_validator()
    errors = validator.Errors()
    objects = validator.build(ROOT / "deploy/k8s/overlays/minikube", errors, set())
    ingress = next(o for o in objects if o["kind"] == "Ingress")
    assert int(ingress["metadata"]["annotations"]["nginx.ingress.kubernetes.io/proxy-read-timeout"]) > 70


@pytest.fixture
def broken_copy(tmp_path, monkeypatch):
    """Копия всего, что читает валидатор, — чтобы портить её, не трогая репозиторий."""
    for name in ("deploy/k8s", "scripts/k8s-up.sh", "scripts/k8s-down.sh", ".env.example", ".dockerignore",
                 "app/core/config.py"):
        source, target = ROOT / name, tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns(".build"))
        else:
            shutil.copy2(source, target)
    validator = load_validator()
    monkeypatch.setattr(validator, "ROOT", tmp_path)
    monkeypatch.setattr(validator, "K8S", tmp_path / "deploy" / "k8s")
    return validator, tmp_path


def errors_of(validator, capsys) -> str:
    assert validator.main([]) == 1
    return capsys.readouterr().err


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} нет в {path}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_validator_catches_service_selector_and_port(broken_copy, capsys):
    validator, root = broken_copy
    web = root / "deploy/k8s/base/web.yaml"
    edit(web, "      targetPort: http", "      targetPort: htp")
    edit(root / "deploy/k8s/base/redis.yaml", "  selector:\n    app.kubernetes.io/name: redis",
         "  selector:\n    app.kubernetes.io/name: rediss")
    err = errors_of(validator, capsys)
    assert "targetPort htp" in err
    assert "Service/redis" in err and "выбирает 0" in err


def test_validator_catches_secret_in_configmap_and_missing_env(broken_copy, capsys):
    validator, root = broken_copy
    config = root / "deploy/k8s/base/config.env"
    config.write_text(config.read_text(encoding="utf-8") + "DEMO_PASSWORD=\n", encoding="utf-8")
    edit(config, "SESSION_DAYS=14\n", "")
    err = errors_of(validator, capsys)
    assert "DEMO_PASSWORD — секрет" in err
    assert "SESSION_DAYS не попадает" in err


def test_validator_catches_two_web_replicas_and_missing_file(broken_copy, capsys):
    validator, root = broken_copy
    edit(root / "deploy/k8s/base/web.yaml", "  replicas: 1", "  replicas: 2")
    edit(root / "deploy/k8s/overlays/k3s/kustomization.yaml", "ingress-class.yaml", "ingress-klass.yaml")
    err = errors_of(validator, capsys)
    assert "replicas должно быть 1" in err
    assert "ingress-klass.yaml не существует" in err


def test_validator_rejects_committed_secret(broken_copy, capsys):
    validator, root = broken_copy
    secret = root / "deploy/k8s/base/secret.yaml"
    secret.write_text("apiVersion: v1\nkind: Secret\nmetadata: {name: lms-secrets}\nstringData: {X: y}\n",
                      encoding="utf-8")
    edit(root / "deploy/k8s/base/kustomization.yaml", "  - ingress.yaml", "  - ingress.yaml\n  - secret.yaml")
    assert "Secret в репозитории запрещён" in errors_of(validator, capsys)


STUB_KUBECTL = """#!/bin/sh
echo "kubectl $*" >> "$STUB_LOG"
for a in "$@"; do
  case "$a" in --from-env-file=*) f=${a#--from-env-file=}; cp "$f" "$STUB_LOG.$(basename "$f")" ;; esac
done
case "$*" in "config current-context") echo default ;; *" -f -"*) cat >/dev/null ;; esac
exit 0
"""


@pytest.mark.skipif(shutil.which("sh") is None, reason="нужен POSIX sh")
def test_up_script_splits_env_and_renders_valid_build(tmp_path, capsys):
    """k8s-up.sh с заглушкой kubectl: секреты не попадают в ConfigMap, итог проходит валидатор."""
    runtime = tmp_path / "runtime"
    shutil.copytree(ROOT / "deploy/k8s", runtime / "deploy/k8s", ignore=shutil.ignore_patterns(".build"))
    (runtime / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/k8s-up.sh", runtime / "scripts/k8s-up.sh")
    # Linux awk сохраняет CR в последнем поле; правила managed должны работать и после Windows checkout.
    keys = runtime / "deploy/k8s/env-keys.txt"
    keys.write_bytes(keys.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "kubectl").write_text(STUB_KUBECTL, encoding="utf-8")
    (bin_dir / "kubectl").chmod(0o755)
    env_file = tmp_path / "test.env"
    env_file.write_text(
        "# комментарий\n"
        "DATABASE_URL=postgresql+asyncpg://lms:x@localhost:5432/lms\n"
        "POSTGRES_PASSWORD=pw_Test-1\n"
        'DEMO_PASSWORD="demo pass"\n'
        "PYRUS_FORM_ID=\n"
        "PUBLIC_BASE_URL=https://lms-university.example\r\n"
        "LLM_PROVIDER=gigachat\n"
        "MY_CUSTOM_TOKEN=s3cr3t\n",
        encoding="utf-8")
    log = tmp_path / "log"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "STUB_LOG": str(log),
           "K8S_CLUSTER": "k3s", "K8S_SKIP_BUILD": "1", "K8S_IMAGE_TAG": "test-tag",
           "K8S_HOST": "lms.example.test", "K8S_ENV_FILE": str(env_file)}
    result = subprocess.run(["sh", str(runtime / "scripts" / "k8s-up.sh")], env=env, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    # Заглушка сохраняет копии файлов, переданных в kubectl create --from-env-file.
    secret_part = Path(f"{log}.secrets.env").read_text(encoding="utf-8")
    plain_part = Path(f"{log}.lms-env.env").read_text(encoding="utf-8")
    assert "kubectl apply -k deploy/k8s/.build" in log.read_text(encoding="utf-8")
    assert "DATABASE_URL=postgresql+asyncpg://lms:pw_Test-1@postgres:5432/lms" in secret_part
    assert "DEMO_PASSWORD=demo pass" in secret_part
    assert "LLM_PROVIDER=gigachat" in plain_part
    # Закрыто по умолчанию: неописанный ключ уходит в Secret с предупреждением, не в ConfigMap.
    assert "MY_CUSTOM_TOKEN=s3cr3t" in secret_part and "MY_CUSTOM_TOKEN" not in plain_part
    assert "ключ MY_CUSTOM_TOKEN не описан" in result.stderr
    for leaked in ("PASSWORD", "DATABASE_URL", "PUBLIC_BASE_URL", "PYRUS_FORM_ID"):
        assert leaked not in plain_part
    # Временные файлы с секретами удалены.
    assert not (runtime / "deploy/k8s/.build/secrets.env").exists()
    assert not (runtime / "deploy/k8s/.build/lms-env.env").exists()

    validator = load_validator()
    validator.ROOT, validator.K8S = runtime, runtime / "deploy/k8s"
    errors = validator.Errors()
    objects = validator.build(runtime / "deploy" / "k8s" / ".build", errors, set())
    validator.check_overlay("k3s", objects, errors)
    assert not errors, errors
    by_name = {(o["kind"], o["metadata"]["name"]): o for o in objects}
    assert by_name[("Ingress", "web")]["spec"]["rules"][0]["host"] == "lms.example.test"
    assert by_name[("Deployment", "bot")]["spec"]["replicas"] == 0  # токена нет — бот не нужен
    web = by_name[("Deployment", "web")]["spec"]["template"]
    assert web["spec"]["containers"][0]["image"] == "mgimo-lms:test-tag"
    assert "mgimo-lms/env-checksum" in web["metadata"]["annotations"]
    config = by_name[("ConfigMap", "lms-config")]["data"]
    assert config["PUBLIC_BASE_URL"] == "http://lms.example.test" and config["COOKIE_SECURE"] == "false"
    build = yaml.safe_load((runtime / "deploy/k8s/.build/kustomization.yaml").read_text(encoding="utf-8"))
    assert build["namespace"] == "mgimo-lms"

    # Доступ через port-forward: ссылки календаря должны вести на localhost.
    env["K8S_PUBLIC_URL"] = "http://localhost:8000"
    result = subprocess.run(["sh", str(runtime / "scripts" / "k8s-up.sh")], env=env, capture_output=True,
                            text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    errors = validator.Errors()
    objects = validator.build(runtime / "deploy" / "k8s" / ".build", errors, set())
    config = next(o for o in objects if o["kind"] == "ConfigMap")["data"]
    assert config["PUBLIC_BASE_URL"] == "http://localhost:8000" and config["COOKIE_SECURE"] == "false"


def test_validator_requires_every_plain_key_listed(broken_copy, capsys):
    validator, root = broken_copy
    edit(root / "deploy/k8s/env-keys.txt", "config SESSION_DAYS\n", "")
    assert "SESSION_DAYS нет в env-keys.txt как config" in errors_of(validator, capsys)
