"""Статическая проверка манифестов Kubernetes кабинета (deploy/k8s, решение 0028).

Живой кластер не нужен. Для каждого overlay манифесты «собираются» упрощённым kustomize
(resources, namespace, configMapGenerator, images, JSON-патчи — ровно то, что используется
в deploy/k8s) и проверяются:

* все kustomization ссылаются на существующие файлы, все YAML разбираются, нет лишних файлов;
* Service <-> Deployment/StatefulSet: селекторы, порты; Ingress -> Service; PVC, ConfigMap, Secret;
* пробы, ресурсы, securityContext, число реплик web/bot, отсутствие секретов в репозитории;
* все переменные .env.example попадают в ConfigMap (base/config.env) или Secret (env-keys.txt),
  а имена совпадают с полями app/core/config.py;
* если установлен pip-пакет kubernetes-validate — ещё и строгая проверка по JSON-схемам API.

    python scripts/k8s-validate.py                   # проверка; код выхода 1 при ошибках
    python scripts/k8s-validate.py --require-schema  # то же, но без kubernetes-validate — ошибка (CI)

Нужен только PyYAML. Приложение не импортируется, поэтому скрипт работает и в CI без зависимостей.
"""

from __future__ import annotations

import argparse
import copy
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
K8S = ROOT / "deploy" / "k8s"
NAMESPACE = "mgimo-lms"
PART_OF = {"app.kubernetes.io/part-of": "mgimo-lms"}
APP_IMAGE = "mgimo-lms"
OVERLAYS = {
    # overlay: (есть ли PostgreSQL в кластере, ingressClassName)
    "k3s": (True, "traefik"),
    "minikube": (True, "nginx"),
    "external-db": (False, "traefik"),
}
# Объекты, которые создаёт scripts/k8s-up.sh, а не kustomize.
SCRIPT_CONFIGMAPS = {"lms-env"}
SCRIPT_SECRETS = {"lms-secrets"}
# Необязательный Secret с сертификатом GigaChat (создаётся руками, docs/kubernetes.md).
OPTIONAL_SECRETS = {"lms-gigachat-ca"}
# Переменные, которых нет в Settings, но которые нужны кластеру.
NON_SETTINGS_KEYS = {"POSTGRES_PASSWORD", "CACHE_URL"}
CLUSTER_SCOPED = {"Namespace"}
SHELL_SCRIPTS = ["scripts/k8s-up.sh", "scripts/k8s-down.sh"]
# Самая старая и свежая из версий, на которых ожидаем запуск (k3s/minikube последних релизов).
SCHEMA_VERSIONS = ("1.29.0", "1.33.0")


class Errors(list):
    def add(self, where: str, message: str) -> None:
        self.append(f"{where}: {message}")


# --- чтение файлов ----------------------------------------------------------------------------

def load_yaml_docs(path: Path, errors: Errors) -> list:
    try:
        return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d is not None]
    except yaml.YAMLError as exc:
        errors.add(rel(path), f"YAML не разбирается: {exc}")
        return []


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def read_env_keys(path: Path) -> dict[str, str]:
    """KEY=VALUE как у kustomize/kubectl --from-env-file: комментарии и пустые строки пропускаются."""
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value
    return result


def read_key_lists(path: Path) -> dict[str, set[str]]:
    lists: dict[str, set[str]] = {"secret": set(), "config": set(), "managed": set(), "envonly": set()}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or parts[0] not in lists:
            raise ValueError(f"{rel(path)}:{number}: ожидается '<secret|config|managed|envonly> KEY', а не {line!r}")
        lists[parts[0]].add(parts[1])
    return lists


def settings_fields(path: Path) -> dict[str, str]:
    """Поля класса Settings и их аннотации — разбором текста, без импорта приложения."""
    fields: dict[str, str] = {}
    inside = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("class Settings"):
            inside = True
            continue
        if inside and line and not line.startswith(" "):
            break
        match = re.match(r"^    ([a-z_][a-z0-9_]*)\s*:\s*([A-Za-z_][A-Za-z0-9_\[\]]*)", line)
        if inside and match and match.group(1) != "model_config":
            fields[match.group(1).upper()] = match.group(2)
    return fields


# --- упрощённый kustomize -----------------------------------------------------------------------

def json_pointer(path: str) -> list:
    return [p.replace("~1", "/").replace("~0", "~") for p in path.lstrip("/").split("/")]


def apply_json_patch(doc: dict, ops: list, where: str, errors: Errors) -> None:
    for op in ops:
        parts = json_pointer(op["path"])
        node = doc
        try:
            for part in parts[:-1]:
                node = node[int(part)] if isinstance(node, list) else node[part]
            last = parts[-1]
            if op["op"] == "add":
                if isinstance(node, list):
                    node.insert(len(node) if last == "-" else int(last), op["value"])
                else:
                    node[last] = op["value"]
            elif op["op"] == "replace":
                if isinstance(node, list):
                    node[int(last)] = op["value"]
                elif last not in node:
                    errors.add(where, f"replace {op['path']}: такого поля нет")
                else:
                    node[last] = op["value"]
            else:
                errors.add(where, f"операция {op['op']} не поддерживается проверкой")
        except (KeyError, IndexError, ValueError, TypeError):
            errors.add(where, f"путь {op['path']} не найден в целевом объекте")


def build(directory: Path, errors: Errors, seen: set[Path]) -> list[dict]:
    """Вернуть объекты kustomization в каталоге (рекурсивно), как сделал бы kustomize build."""
    kfile = directory / "kustomization.yaml"
    where = rel(kfile)
    if not kfile.is_file():
        errors.add(rel(directory), "нет kustomization.yaml")
        return []
    seen.add(kfile)
    docs = load_yaml_docs(kfile, errors)
    if len(docs) != 1 or not isinstance(docs[0], dict):
        errors.add(where, "ожидается один документ")
        return []
    k = docs[0]
    if k.get("kind") != "Kustomization" or not str(k.get("apiVersion", "")).startswith("kustomize.config.k8s.io/"):
        errors.add(where, "kind/apiVersion не Kustomization")
    known = {"apiVersion", "kind", "namespace", "resources", "patches", "configMapGenerator",
             "generatorOptions", "images", "replicas"}
    for key in set(k) - known:
        errors.add(where, f"поле {key!r} проверкой не поддерживается — расширьте scripts/k8s-validate.py")

    objects: list[dict] = []
    for item in k.get("resources") or []:
        target = (directory / item).resolve()
        if target.is_dir():
            objects += build(target, errors, seen)
        elif target.is_file():
            seen.add(target)
            for doc in load_yaml_docs(target, errors):
                if not isinstance(doc, dict):
                    errors.add(rel(target), "документ не является объектом")
                    continue
                doc.setdefault("metadata", {})
                doc["__file"] = rel(target)
                objects.append(doc)
        else:
            errors.add(where, f"resources: {item} не существует")

    options = k.get("generatorOptions") or {}
    for gen in k.get("configMapGenerator") or []:
        data: dict[str, str] = {}
        for env in gen.get("envs") or []:
            local = within_root(directory, env, where, errors)
            if local:
                seen.add(local)
                data.update(read_env_keys(local))
        for literal in gen.get("literals") or []:
            key, value = literal.split("=", 1)
            data[key] = value
        if gen.get("behavior", "create") == "merge":
            matches = [o for o in objects if o.get("kind") == "ConfigMap" and o["metadata"].get("name") == gen["name"]]
            if not matches:
                errors.add(where, f"configMapGenerator merge: нет ConfigMap {gen['name']}")
            for match in matches:
                match.setdefault("data", {}).update(data)
            continue
        objects.append({
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": gen["name"], "labels": dict(options.get("labels") or {})},
            "data": data, "__file": where, "__generated": True,
        })

    namespace = k.get("namespace")
    if namespace:
        for obj in objects:
            if obj.get("kind") not in CLUSTER_SCOPED:
                obj["metadata"]["namespace"] = namespace

    for image in k.get("images") or []:
        for container in all_containers(objects):
            name = str(container.get("image", "")).split(":", 1)[0]
            if name == image["name"]:
                container["image"] = f"{image.get('newName', name)}:{image.get('newTag', 'latest')}"

    for patch in k.get("patches") or []:
        target = patch.get("target") or {}
        matches = [o for o in objects if o.get("kind") == target.get("kind")
                   and o["metadata"].get("name") == target.get("name")]
        if not matches:
            errors.add(where, f"patch: цель {target} не найдена среди ресурсов")
        if "path" in patch:
            local = within_root(directory, patch["path"], where, errors)
            if not local:
                continue
            seen.add(local)
            ops = load_yaml_docs(local, errors)
            ops = ops[0] if ops else []
        else:
            ops = yaml.safe_load(patch.get("patch", "")) or []
        for match in matches:
            if isinstance(ops, list):
                apply_json_patch(match, ops, where, errors)
            elif isinstance(ops, dict):
                # Strategic merge — в упрощённом виде: словари сливаются, списки заменяются.
                body = {k: v for k, v in ops.items() if k not in {"apiVersion", "kind", "metadata"}}
                deep_merge(match, body)
                deep_merge(match["metadata"], {k: v for k, v in (ops.get("metadata") or {}).items() if k != "name"})
            else:
                errors.add(where, "patch: ожидается список JSON-операций или объект")

    for item in k.get("replicas") or []:
        for obj in objects:
            if obj["metadata"].get("name") == item["name"] and obj.get("kind") in {"Deployment", "StatefulSet"}:
                obj["spec"]["replicas"] = item["count"]
    return objects


def deep_merge(target: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def within_root(directory: Path, name: str, where: str, errors: Errors) -> Path | None:
    """kustomize по умолчанию запрещает файлы вне каталога kustomization (load restrictor)."""
    target = (directory / name).resolve()
    if directory.resolve() not in target.parents:
        errors.add(where, f"{name}: файл вне каталога kustomization — kustomize откажется его читать")
        return None
    if not target.is_file():
        errors.add(where, f"{name} не существует")
        return None
    return target


# --- проверки объектов --------------------------------------------------------------------------

def pod_spec(obj: dict) -> dict | None:
    if obj.get("kind") in {"Deployment", "StatefulSet"}:
        return obj.get("spec", {}).get("template", {}).get("spec", {})
    return None


def all_containers(objects: list[dict]):
    for obj in objects:
        spec = pod_spec(obj)
        if spec:
            yield from spec.get("initContainers") or []
            yield from spec.get("containers") or []


def name_of(obj: dict) -> str:
    return f"{obj.get('kind')}/{obj['metadata'].get('name')} ({obj.get('__file')})"


def selector_matches(selector: dict, labels: dict) -> bool:
    """Селектор Service (простой словарь) или LabelSelector (matchLabels/matchExpressions)."""
    if not selector:
        return False
    structured = "matchLabels" in selector or "matchExpressions" in selector
    plain = (selector.get("matchLabels") or {}) if structured else selector
    if any(labels.get(key) != value for key, value in plain.items()):
        return False
    for expr in (selector.get("matchExpressions") or []) if structured else []:
        value = labels.get(expr["key"])
        if expr["operator"] == "In" and value not in expr["values"]:
            return False
        if expr["operator"] == "NotIn" and value in expr["values"]:
            return False
        if expr["operator"] == "Exists" and value is None:
            return False
    return True


def check_overlay(overlay: str, objects: list[dict], errors: Errors) -> None:
    has_db, ingress_class = OVERLAYS[overlay]
    by_kind: dict[str, dict[str, dict]] = {}
    for obj in objects:
        where = f"[{overlay}] {name_of(obj)}"
        for field in ("apiVersion", "kind"):
            if not obj.get(field):
                errors.add(where, f"нет {field}")
        name = obj["metadata"].get("name")
        if not name:
            errors.add(where, "нет metadata.name")
            continue
        if name in by_kind.setdefault(obj["kind"], {}):
            errors.add(where, "объект с таким kind/name уже есть")
        by_kind[obj["kind"]][name] = obj
        if obj["kind"] not in CLUSTER_SCOPED and obj["metadata"].get("namespace") != NAMESPACE:
            errors.add(where, f"namespace должен быть {NAMESPACE}, а не {obj['metadata'].get('namespace')}")
        labels = obj["metadata"].get("labels") or {}
        if any(labels.get(k) != v for k, v in PART_OF.items()):
            errors.add(where, "нет метки app.kubernetes.io/part-of=mgimo-lms (по ней работает k8s-down.sh)")
        if obj["kind"] == "Secret":
            errors.add(where, "Secret в репозитории запрещён: секреты создаёт scripts/k8s-up.sh из .env")

    workloads = {**by_kind.get("Deployment", {}), **by_kind.get("StatefulSet", {})}
    configmaps = by_kind.get("ConfigMap", {})
    services = by_kind.get("Service", {})
    pvcs = by_kind.get("PersistentVolumeClaim", {})

    expected = {"web", "bot", "redis"} | ({"postgres"} if has_db else set())
    if set(workloads) != expected:
        errors.add(f"[{overlay}]", f"нагрузки {sorted(workloads)}, ожидались {sorted(expected)}")
    if "Namespace" not in by_kind or NAMESPACE not in by_kind["Namespace"]:
        errors.add(f"[{overlay}]", f"нет Namespace {NAMESPACE}")

    for name, obj in workloads.items():
        where = f"[{overlay}] {name_of(obj)}"
        spec = obj.get("spec", {})
        template_labels = spec.get("template", {}).get("metadata", {}).get("labels") or {}
        if not selector_matches(spec.get("selector") or {}, template_labels):
            errors.add(where, "spec.selector не совпадает с метками шаблона пода")
        pod = pod_spec(obj) or {}
        pod_sc = pod.get("securityContext") or {}
        if pod_sc.get("runAsNonRoot") is not True:
            errors.add(where, "нет securityContext.runAsNonRoot: true")
        if pod.get("automountServiceAccountToken") is not False:
            errors.add(where, "токен ServiceAccount не нужен приложению: automountServiceAccountToken: false")
        volumes = {v["name"]: v for v in pod.get("volumes") or []}
        claim_templates = {t["metadata"]["name"] for t in spec.get("volumeClaimTemplates") or []}
        for volume in volumes.values():
            if "persistentVolumeClaim" in volume and volume["persistentVolumeClaim"]["claimName"] not in pvcs:
                errors.add(where, f"том {volume['name']}: нет PVC {volume['persistentVolumeClaim']['claimName']}")
            if "configMap" in volume and volume["configMap"]["name"] not in configmaps:
                errors.add(where, f"том {volume['name']}: нет ConfigMap {volume['configMap']['name']}")
            if "secret" in volume:
                secret = volume["secret"]["secretName"]
                if secret not in OPTIONAL_SECRETS | SCRIPT_SECRETS:
                    errors.add(where, f"том {volume['name']}: неизвестный Secret {secret}")
                if secret in OPTIONAL_SECRETS and volume["secret"].get("optional") is not True:
                    errors.add(where, f"Secret {secret} необязательный — нужен optional: true")
        for container in (pod.get("initContainers") or []) + (pod.get("containers") or []):
            check_container(f"{where} контейнер {container.get('name')}", container, pod_sc, volumes,
                            claim_templates, configmaps, errors)
        if obj["kind"] == "StatefulSet" and spec.get("serviceName") not in services:
            errors.add(where, f"serviceName {spec.get('serviceName')}: нет такого Service")

    # Требования, на которых держится корректность приложения.
    for name in ("web", "bot"):
        obj = workloads.get(name)
        if not obj:
            continue
        where = f"[{overlay}] {name_of(obj)}"
        # Бот без токена k8s-up.sh выключает (replicas: 0); больше одного — никогда.
        allowed = {1} if name == "web" else {0, 1}
        if obj["spec"].get("replicas") not in allowed:
            errors.add(where, "replicas должно быть 1 (web: сверка Pyrus в процессе; bot: long polling)")
        if (obj["spec"].get("strategy") or {}).get("type") != "Recreate":
            errors.add(where, "strategy должна быть Recreate: два процесса одновременно недопустимы")
    web = workloads.get("web")
    if web:
        pod = pod_spec(web)
        inits = pod.get("initContainers") or []
        if not any("alembic upgrade head" in " ".join(c.get("command") or []) for c in inits):
            errors.add(f"[{overlay}] web", "нет initContainer с alembic upgrade head")
        for container in pod.get("containers") or []:
            if "alembic" in " ".join(container.get("command") or []):
                errors.add(f"[{overlay}] web", "миграции в основном контейнере — только в initContainer")
            for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
                if (container.get(probe) or {}).get("httpGet", {}).get("path") != "/healthz":
                    errors.add(f"[{overlay}] web", f"{probe} должна ходить на /healthz")
    bot = workloads.get("bot")
    if bot:
        bot_labels = bot["spec"]["template"]["metadata"].get("labels") or {}
        for svc in services.values():
            if selector_matches(svc["spec"].get("selector") or {}, bot_labels):
                errors.add(f"[{overlay}] {name_of(svc)}", "у бота не должно быть Service")

    for name, svc in services.items():
        where = f"[{overlay}] {name_of(svc)}"
        spec = svc.get("spec", {})
        if spec.get("type", "ClusterIP") != "ClusterIP":
            errors.add(where, "наружу публикуется только web через Ingress; Service — ClusterIP")
        targets = [w for w in workloads.values()
                   if selector_matches(spec.get("selector") or {}, w["spec"]["template"]["metadata"].get("labels") or {})]
        if len(targets) != 1:
            errors.add(where, f"селектор выбирает {len(targets)} нагрузок, ожидается ровно одна")
            continue
        ports = container_ports(targets[0])
        for port in spec.get("ports") or []:
            target = port.get("targetPort", port.get("port"))
            if target not in ports and target not in ports.values():
                errors.add(where, f"targetPort {target} нет среди портов контейнеров {name_of(targets[0])}")

    for ing in by_kind.get("Ingress", {}).values():
        where = f"[{overlay}] {name_of(ing)}"
        if ing["spec"].get("ingressClassName") != ingress_class:
            errors.add(where, f"ingressClassName должен быть {ingress_class}")
        for rule in ing["spec"].get("rules") or []:
            for path in rule.get("http", {}).get("paths") or []:
                backend = path["backend"]["service"]
                svc = services.get(backend["name"])
                if not svc:
                    errors.add(where, f"backend {backend['name']}: нет такого Service")
                    continue
                port = backend.get("port") or {}
                names = {p.get("name") for p in svc["spec"].get("ports") or []}
                numbers = {p.get("port") for p in svc["spec"].get("ports") or []}
                if port.get("name") not in names and port.get("number") not in numbers:
                    errors.add(where, f"порт {port} нет в Service {backend['name']}")
                if backend["name"] != "web":
                    errors.add(where, "Ingress только для web")

    for policy in by_kind.get("NetworkPolicy", {}).values():
        where = f"[{overlay}] {name_of(policy)}"
        selector = policy["spec"].get("podSelector") or {}
        if not any(selector_matches(selector, w["spec"]["template"]["metadata"].get("labels") or {})
                   for w in workloads.values()):
            errors.add(where, "podSelector не выбирает ни один под")
        for rule in policy["spec"].get("ingress") or []:
            for source in rule.get("from") or []:
                sel = source.get("podSelector")
                if sel is not None and not any(selector_matches(sel, w["spec"]["template"]["metadata"].get("labels") or {})
                                               for w in workloads.values()):
                    errors.add(where, "from.podSelector не выбирает ни один под")


def container_ports(workload: dict) -> dict:
    ports = {}
    for container in pod_spec(workload).get("containers") or []:
        for port in container.get("ports") or []:
            ports[port.get("name", port["containerPort"])] = port["containerPort"]
    return ports


def check_container(where: str, container: dict, pod_sc: dict, volumes: dict, claim_templates: set,
                    configmaps: dict, errors: Errors) -> None:
    resources = container.get("resources") or {}
    for section in ("requests", "limits"):
        for resource in ("cpu", "memory"):
            if resource not in (resources.get(section) or {}):
                errors.add(where, f"нет resources.{section}.{resource}")
    sc = container.get("securityContext") or {}
    if sc.get("allowPrivilegeEscalation") is not False:
        errors.add(where, "нужно allowPrivilegeEscalation: false")
    if "ALL" not in ((sc.get("capabilities") or {}).get("drop") or []):
        errors.add(where, "нужно capabilities.drop: [ALL]")
    image = str(container.get("image", ""))
    if image.split(":", 1)[0] == APP_IMAGE:
        if container.get("imagePullPolicy") != "Never":
            errors.add(where, "образ кабинета локальный: imagePullPolicy: Never")
        if pod_sc.get("runAsUser") != 1000:
            errors.add(where, "образ кабинета работает под uid 1000 (Dockerfile): runAsUser: 1000")
        if sc.get("readOnlyRootFilesystem") is not True:
            errors.add(where, "для образа кабинета ожидается readOnlyRootFilesystem: true")
    elif ":" not in image:
        errors.add(where, f"у внешнего образа {image} должен быть тег")
    ports = {p.get("name") for p in container.get("ports") or []}
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        http = (container.get(probe) or {}).get("httpGet")
        if http and http.get("port") not in ports and not isinstance(http.get("port"), int):
            errors.add(where, f"{probe}: порта {http.get('port')} нет у контейнера")
    for mount in container.get("volumeMounts") or []:
        if mount["name"] not in volumes and mount["name"] not in claim_templates:
            errors.add(where, f"volumeMount {mount['name']}: нет такого тома")
    for source in container.get("envFrom") or []:
        if "configMapRef" in source:
            ref = source["configMapRef"]
            if ref["name"] in SCRIPT_CONFIGMAPS:
                if ref.get("optional") is not True:
                    errors.add(where, f"ConfigMap {ref['name']} создаёт скрипт — нужен optional: true")
            elif ref["name"] not in configmaps:
                errors.add(where, f"envFrom: нет ConfigMap {ref['name']}")
        if "secretRef" in source and source["secretRef"]["name"] not in SCRIPT_SECRETS:
            errors.add(where, f"envFrom: неизвестный Secret {source['secretRef']['name']}")
    for env in container.get("env") or []:
        ref = (env.get("valueFrom") or {})
        if "configMapKeyRef" in ref:
            cm = configmaps.get(ref["configMapKeyRef"]["name"])
            if not cm or ref["configMapKeyRef"]["key"] not in (cm.get("data") or {}):
                errors.add(where, f"env {env['name']}: нет ключа {ref['configMapKeyRef']} в ConfigMap")
        if "secretKeyRef" in ref and ref["secretKeyRef"]["name"] not in SCRIPT_SECRETS:
            errors.add(where, f"env {env['name']}: неизвестный Secret {ref['secretKeyRef']['name']}")


def check_schema(overlay: str, objects: list[dict], errors: Errors, require: bool) -> bool:
    try:
        import kubernetes_validate
    except ImportError:
        if require:
            errors.add("schema", "нет pip-пакета kubernetes-validate (pip install kubernetes-validate)")
        return False
    for obj in objects:
        clean = {k: v for k, v in obj.items() if not k.startswith("__")}
        for version in SCHEMA_VERSIONS:
            try:
                kubernetes_validate.validate(clean, version, strict=True)
            except kubernetes_validate.ValidationError as exc:
                errors.add(f"[{overlay}] {name_of(obj)}", f"схема Kubernetes {version}: {exc.message}")
            except kubernetes_validate.SchemaNotFoundError as exc:
                errors.add(f"[{overlay}] {name_of(obj)}", f"нет схемы {version}: {exc}")
    return True


# --- переменные окружения ---------------------------------------------------------------------

def check_env(errors: Errors) -> None:
    example = read_env_keys(ROOT / ".env.example")
    config = read_env_keys(K8S / "base" / "config.env")
    try:
        lists = read_key_lists(K8S / "env-keys.txt")
    except ValueError as exc:
        errors.add("env-keys.txt", str(exc))
        return
    secret, managed, envonly = lists["secret"], lists["managed"], lists["envonly"]
    plain = lists["config"]
    fields = settings_fields(ROOT / "app" / "core" / "config.py")
    if not fields:
        errors.add("app/core/config.py", "не удалось найти поля Settings")
        return

    for key in example:
        if key not in config and key not in secret and key not in envonly:
            errors.add(".env.example", f"{key} не попадает ни в base/config.env, ни в Secret (env-keys.txt)")
    # Закрыто по умолчанию: k8s-up.sh кладёт в ConfigMap только config/envonly, остальное — в Secret.
    for key in example:
        if key not in secret | plain | envonly | managed:
            errors.add("env-keys.txt", f"{key} из .env.example не описан — k8s-up.sh положит его в Secret")
    for kind_a, kind_b in (("secret", "config"), ("secret", "envonly"), ("config", "envonly"),
                           ("config", "managed"), ("envonly", "managed")):
        for key in sorted(lists[kind_a] & lists[kind_b]):
            errors.add("env-keys.txt", f"{key} одновременно {kind_a} и {kind_b}")
    for key in sorted(secret & managed - {"DATABASE_URL"}):
        errors.add("env-keys.txt", f"{key} одновременно secret и managed")
    for key in sorted(set(config) - plain - managed):
        errors.add("base/config.env", f"{key} нет в env-keys.txt как config — из .env он ушёл бы в Secret")
    for key in sorted(set(config) & secret):
        errors.add("base/config.env", f"{key} — секрет, ему не место в ConfigMap")
    for key in sorted(set(config) & envonly):
        errors.add("base/config.env", f"{key} помечен envonly и не должен иметь значения по умолчанию")
    for key in sorted(secret - set(example)):
        errors.add("env-keys.txt", f"секрет {key} отсутствует в .env.example — шаблон неполный")
    template = read_env_keys(K8S / "secrets.env.example")
    template_text = (K8S / "secrets.env.example").read_text(encoding="utf-8")
    for key in sorted(secret - {"DATABASE_URL"}):
        if key not in template:
            errors.add("secrets.env.example", f"нет ключа {key}")
    if "DATABASE_URL" not in template_text:
        errors.add("secrets.env.example", "не упомянут DATABASE_URL для внешней БД")
    for key, value in template.items():
        if value.strip():
            errors.add("secrets.env.example", f"{key}: в шаблоне не должно быть значения")
    for key in sorted(set(config) | secret | plain | envonly | managed):
        if key not in fields and key not in NON_SETTINGS_KEYS:
            errors.add("deploy/k8s", f"{key} нет среди полей Settings (app/core/config.py) — опечатка?")
    for key, value in config.items():
        if value == "" and fields.get(key) in {"int", "float", "bool"}:
            errors.add("base/config.env", f"{key}: пустое значение не разберётся как {fields[key]}")
    for key in sorted(managed - {"DATABASE_URL"}):
        if key not in config:
            errors.add("base/config.env", f"managed-ключ {key} должен иметь значение для кластера")
    required = {name for name, line in _required_fields(ROOT / "app" / "core" / "config.py")}
    for key in sorted(required):
        if key not in secret and key not in config:
            errors.add("deploy/k8s", f"обязательная настройка {key} не задаётся ни ConfigMap, ни Secret")
    if config.get("CACHE_URL") != "redis://redis:6379/0":
        errors.add("base/config.env", "CACHE_URL должен быть redis://redis:6379/0 (Service redis)")


def _required_fields(path: Path):
    """Поля Settings без значения по умолчанию (например, database_url: str)."""
    inside = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("class Settings"):
            inside = True
            continue
        if inside and line and not line.startswith(" "):
            break
        match = re.match(r"^    ([a-z_][a-z0-9_]*)\s*:\s*[^=]+$", line)
        if inside and match and match.group(1) != "model_config":
            yield match.group(1).upper(), line


# --- файлы и скрипты ---------------------------------------------------------------------------

def check_repository(errors: Errors, seen: set[Path]) -> None:
    for path in sorted(K8S.rglob("*")):
        if ".build" in path.relative_to(K8S).parts or not path.is_file():
            continue
        if path.suffix in {".yaml", ".yml"}:
            for doc in load_yaml_docs(path, errors):
                if isinstance(doc, dict) and doc.get("kind") == "Secret":
                    errors.add(rel(path), "Secret в репозитории запрещён")
            if path.resolve() not in seen:
                errors.add(rel(path), "файл не подключён ни в одну kustomization")
    ignore = (K8S / ".gitignore").read_text(encoding="utf-8").splitlines() if (K8S / ".gitignore").is_file() else []
    for pattern in (".build/", "secrets.env"):
        if pattern not in ignore:
            errors.add("deploy/k8s/.gitignore", f"нет строки {pattern}")
    up = (ROOT / "scripts" / "k8s-up.sh").read_text(encoding="utf-8")
    for needle in ("env-keys.txt", "set -eu", "--from-env-file", "kubectl apply -k", "rollout status",
                   # закрыто по умолчанию: в ConfigMap — только config/envonly
                   'in_list config "$key" || in_list envonly "$key"',
                   # временная kustomization с namespace (merge над lms-config из base)
                   'echo "namespace: $NS"',
                   # секреты — во временном каталоге вне контекста docker build
                   "mktemp -d"):
        if needle not in up:
            errors.add("scripts/k8s-up.sh", f"ожидается {needle!r}")
    if "--untracked-files=no" in up:
        errors.add("scripts/k8s-up.sh", "неотслеживаемые файлы должны помечать образ как изменённый")
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines() \
        if (ROOT / ".dockerignore").is_file() else []
    for pattern in ("deploy/k8s/.build", "deploy/k8s/secrets.env"):
        if pattern not in dockerignore:
            errors.add(".dockerignore", f"нет строки {pattern} — секреты попали бы в контекст сборки")
    for overlay in OVERLAYS:
        if not (K8S / "overlays" / overlay / "kustomization.yaml").is_file():
            errors.add("deploy/k8s/overlays", f"нет overlay {overlay}")
    sh = shutil.which("sh")
    for script in SHELL_SCRIPTS:
        path = ROOT / script
        if not path.is_file():
            errors.add(script, "нет файла")
            continue
        if not path.read_text(encoding="utf-8").startswith("#!/bin/sh\n"):
            errors.add(script, "ожидается POSIX sh (#!/bin/sh)")
        if sh:
            result = subprocess.run([sh, "-n", str(path)], capture_output=True, text=True)
            if result.returncode != 0:
                errors.add(script, f"sh -n: {result.stderr.strip()}")


def render(overlay: str, errors: Errors, seen: set[Path]) -> list[dict]:
    return build(K8S / "overlays" / overlay, errors, seen)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--require-schema", action="store_true",
                        help="считать ошибкой отсутствие pip-пакета kubernetes-validate")
    args = parser.parse_args(argv)

    errors = Errors()
    seen: set[Path] = set()
    schema_checked = False
    counts = {}
    for overlay in OVERLAYS:
        objects = render(overlay, errors, seen)
        counts[overlay] = len(objects)
        check_overlay(overlay, copy.deepcopy(objects), errors)
        schema_checked = check_schema(overlay, objects, errors, args.require_schema) or schema_checked
    check_env(errors)
    check_repository(errors, {p.resolve() for p in seen})

    if errors:
        print(f"deploy/k8s: найдено ошибок — {len(errors)}", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    summary = ", ".join(f"{name}: {count} объектов" for name, count in counts.items())
    schema = f"схемы Kubernetes {', '.join(SCHEMA_VERSIONS)} проверены" if schema_checked else \
        "схемы не проверялись (нет pip-пакета kubernetes-validate)"
    print(f"deploy/k8s: OK ({summary}; {schema})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
