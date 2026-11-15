"""Приложение FastAPI: экраны кабинета студента, проверка живости и вебхук Pyrus.

    uvicorn app.main:app --port 8000

Фоновая сверка заявок с Pyrus запускается здесь же, раз в PYRUS_SYNC_INTERVAL секунд.
Рассчитано на один процесс uvicorn: при нескольких воркерах сверка шла бы в каждом.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.certificates.pyrus_client import PyrusClient
from app.assistant.provider import GigaChat
from app.assistant.service import Assistant
from app.web.assistant_views import router as assistant_router
from app.certificates.service import CertificateSync
from app.certificates.webhook import router as pyrus_router
from app.core.config import Settings, get_settings
from app.db.session import make_engine, make_session_factory
from app.web.deps import LoginRequired
from app.web.notification_views import router as notif_router
from app.web.notification_views import staff_router, teacher_router as teacher_ann_router
from app.web.staff_views import router as staff_home_router
from app.web.teacher_views import router as teacher_router
from app.web.views import router as web_router

log = logging.getLogger(__name__)


async def _sync_loop(sync: CertificateSync, interval: int) -> None:
    while True:
        try:
            report = await sync.run()
            if report.changed or report.push_failed or report.replies_failed or report.paper_failed:
                log.info("сверка с Pyrus: %s", report)
        except Exception:
            log.exception("сверка с Pyrus упала")
        await asyncio.sleep(interval)


def create_app(settings: Settings | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        cfg = settings or get_settings()
        app.state.settings = cfg
        provider = None
        if cfg.llm_provider == "gigachat" and cfg.gigachat_auth_key.get_secret_value():
            try:
                provider = GigaChat(cfg.gigachat_auth_key.get_secret_value(), cfg.gigachat_scope,
                                    cfg.gigachat_model, cfg.gigachat_ca_bundle)
            except (OSError, ValueError):
                log.error("Ассистент отключён: проверьте сертификат GigaChat")
        app.state.assistant = Assistant(cfg, provider)
        engine = make_engine(cfg.database_url)
        app.state.session_factory = make_session_factory(engine)
        app.state.certificate_sync = None
        app.state.pyrus_security_key = cfg.pyrus_security_key
        client = None
        loop_task = None
        if cfg.pyrus_enabled:
            client = PyrusClient(cfg.pyrus_login, cfg.pyrus_security_key, auth_url=cfg.pyrus_auth_url)
            app.state.certificate_sync = CertificateSync(
                app.state.session_factory, client, form_id=cfg.pyrus_form_id,
                font_path=cfg.pdf_font_path, download_days=cfg.cert_download_days,
            )
            if cfg.pyrus_sync_interval > 0:
                loop_task = asyncio.create_task(_sync_loop(app.state.certificate_sync, cfg.pyrus_sync_interval))
        else:
            log.warning("Pyrus не настроен (PYRUS_LOGIN / PYRUS_SECURITY_KEY / PYRUS_FORM_ID) — заявки копятся в очереди")
        try:
            yield
        finally:
            await app.state.assistant.aclose()
            if loop_task is not None:
                loop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await loop_task
            if client is not None:
                await client.aclose()
            await engine.dispose()

    app = FastAPI(title="MGIMO-LMS (концепт для конкурса)", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"ok": True}

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, exc: LoginRequired) -> Response:
        if request.headers.get("HX-Request"):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web" / "static"), name="static")
    app.include_router(pyrus_router)
    app.include_router(assistant_router)
    app.include_router(web_router)
    app.include_router(teacher_router)
    app.include_router(notif_router)
    app.include_router(staff_router)
    app.include_router(staff_home_router)
    app.include_router(teacher_ann_router)
    return app


app = create_app()
