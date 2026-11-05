"""Входящий вебхук бота Pyrus: POST /integrations/pyrus/webhook.

Pyrus вызывает бота, когда задача попадает к нему (бот — участник маршрута формы).
Тело вебхука не принимается на веру: после проверки подписи берём из него только
task_id, ищем заявку по pyrus_task_id в нашей БД и перечитываем задачу через API.
Отвечаем 200 сразу (Pyrus ждёт не дольше 60 с и повторяет 3 раза), работа — в фоне.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.certificates.pyrus_client import verify_signature

log = logging.getLogger(__name__)

router = APIRouter()


@router.post("/integrations/pyrus/webhook")
async def pyrus_webhook(request: Request, background: BackgroundTasks) -> dict:
    state = request.app.state
    sync = getattr(state, "certificate_sync", None)
    if sync is None:
        raise HTTPException(status_code=404)
    body = await request.body()
    if not verify_signature(body, request.headers.get("X-Pyrus-Sig"), state.pyrus_security_key):
        raise HTTPException(status_code=403, detail="bad signature")
    try:
        payload = json.loads(body)
        task_id = int(payload.get("task_id") or payload["task"]["id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="no task_id")
    background.add_task(_refresh, sync, task_id)
    # Пустое тело: бот ничего не пишет в задачу в ответ на событие.
    return {}


async def _refresh(sync, task_id: int) -> None:
    try:
        known = await sync.refresh_task(task_id)
        if known:
            await sync.push_ready_pdfs()
        else:
            log.info("вебхук Pyrus по незнакомой задаче %s — пропускаем", task_id)
    except Exception:  # фоновая задача: ошибку только логируем, сверка подберёт
        log.exception("обработка вебхука Pyrus по задаче %s упала", task_id)
