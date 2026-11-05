"""Поддельный Pyrus API v4 для тестов: авторизация, задачи, комментарии, загрузка файлов.

Формы ответов — по документации https://pyrus.com/en/help/api (см. решение 0003).
Тест управляет «сотрудником деканата» через методы set_field / reject / finish.
"""

from __future__ import annotations

import json
import re
import uuid

import httpx

AUTH_URL = "https://accounts.pyrus.test/api/v4/auth"
API_URL = "https://api.pyrus.test/v4/"


class FakePyrus:
    def __init__(self) -> None:
        self.tasks: dict[int, dict] = {}
        self.files: dict[str, tuple[str, bytes]] = {}
        self.next_id = 1000
        self.down = False
        self.token_seq = 0
        self.valid_tokens: set[str] = set()
        self.auth_calls = 0
        self.log: list[tuple[str, str]] = []

    # --- «сотрудник деканата» ---

    def _upsert_field(self, task_id: int, name: str, field: dict) -> None:
        fields = self.tasks[task_id]["fields"]
        for i, f in enumerate(fields):
            if f["name"] == name:
                fields[i] = field
                return
        fields.append(field)

    def set_status(self, task_id: int, choice: str) -> None:
        self._upsert_field(task_id, "Статус для студента", {
            "id": 90, "type": "multiple_choice", "name": "Статус для студента",
            "value": {"choice_ids": [1], "choice_names": [choice]},
        })

    def set_message(self, task_id: int, text: str) -> None:
        self._upsert_field(task_id, "Сообщение студенту", {
            "id": 91, "type": "text", "name": "Сообщение студенту", "value": text,
        })

    def set_paper(self, task_id: int, choice: str) -> None:
        self._upsert_field(task_id, "Бумажный оригинал", {
            "id": 92, "type": "multiple_choice", "name": "Бумажный оригинал",
            "value": {"choice_ids": [1], "choice_names": [choice]},
        })

    def reject(self, task_id: int) -> None:
        self.tasks[task_id]["approvals"][0][0]["approval_choice"] = "rejected"

    def finish(self, task_id: int) -> None:
        self.tasks[task_id]["comments"].append({"id": 1, "text": "Готово", "action": "finished"})

    def revoke_tokens(self) -> None:
        self.valid_tokens.clear()

    def field(self, task_id: int, name: str):
        for f in self.tasks[task_id]["fields"]:
            if f["name"] == name:
                return f["value"]
        return None

    # --- HTTP ---

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.log.append((request.method, url))
        if self.down:
            return httpx.Response(503, json={"error_code": "server_error", "error": "down"})
        if url == AUTH_URL:
            return self._auth(request)
        if not url.startswith(API_URL):
            return httpx.Response(404)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.valid_tokens:
            return httpx.Response(401, json={"error_code": "expired_token", "error": "expired"})
        path = url[len(API_URL):].rstrip("/")
        if request.method == "POST" and path == "tasks":
            return self._create(json.loads(request.content))
        if request.method == "POST" and path == "files/upload":
            return self._upload(request)
        m = re.fullmatch(r"tasks/(\d+)(/comments)?", path)
        if m and int(m.group(1)) in self.tasks:
            task = self.tasks[int(m.group(1))]
            if m.group(2) and request.method == "POST":
                return self._comment(task, json.loads(request.content))
            if request.method == "GET":
                return httpx.Response(200, json={"task": task})
        return httpx.Response(404, json={"error_code": "not_found", "error": "no such"})

    def _auth(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body != {"login": "bot@test", "security_key": "secret"}:
            return httpx.Response(401, json={"error_code": "invalid_credentials", "error": "bad"})
        self.auth_calls += 1
        self.token_seq += 1
        token = f"tok-{self.token_seq}"
        self.valid_tokens.add(token)
        return httpx.Response(200, json={"access_token": token, "api_url": API_URL, "files_url": "https://files.pyrus.test/"})

    def _create(self, body: dict) -> httpx.Response:
        if body.get("form_id") != 321:
            return httpx.Response(400, json={"error_code": "form_id_missing", "error": "form"})
        self.next_id += 1
        fields = [
            {"id": i, "type": "text", "name": f["name"], "value": f["value"]}
            for i, f in enumerate(body["fields"], start=1)
        ]
        task = {
            "id": self.next_id, "form_id": 321, "fields": fields,
            "approvals": [[{"person": {"id": 1}, "approval_choice": "waiting"}]],
            "comments": [],
        }
        self.tasks[task["id"]] = task
        return httpx.Response(200, json={"task": task})

    def _comment(self, task: dict, body: dict) -> httpx.Response:
        attachments = []
        for a in body.get("attachments", []):
            guid = a["guid"] if isinstance(a, dict) else a
            if guid not in self.files:
                return httpx.Response(400, json={"error_code": "unrecognized_attachment_id", "error": "att"})
            attachments.append({"id": len(self.files), "name": self.files[guid][0]})
        comment = {"id": len(task["comments"]) + 2, "text": body.get("text"), "attachments": attachments}
        if body.get("action"):
            comment["action"] = body["action"]
        task["comments"].append(comment)
        return httpx.Response(200, json={"task": task})

    def _upload(self, request: httpx.Request) -> httpx.Response:
        content = request.content
        m = re.search(rb'name="file"; filename="([^"]+)"\r\n(?:[^\r\n]*\r\n)*\r\n', content)
        if m is None:
            return httpx.Response(400, json={"error_code": "empty_file", "error": "no file"})
        boundary = request.headers["content-type"].split("boundary=")[1].encode()
        data = content[m.end():content.rindex(b"\r\n--" + boundary)]
        guid = str(uuid.uuid4())
        self.files[guid] = (m.group(1).decode(), data)
        return httpx.Response(200, json={"guid": guid, "md5_hash": "x"})
