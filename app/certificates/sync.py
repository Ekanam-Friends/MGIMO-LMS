"""Сверка заявок на справки с Pyrus вручную.

    python -m app.certificates.sync           # один проход: отправить очередь, перечитать открытые
    python -m app.certificates.sync --loop    # бесконечно, раз в PYRUS_SYNC_INTERVAL секунд

Обычно не нужна: то же самое делает приложение (app/main.py) в фоне.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.certificates.pyrus_client import PyrusClient
from app.certificates.service import CertificateSync
from app.core.config import get_settings
from app.db.session import make_engine, make_session_factory


async def _main(loop: bool) -> int:
    cfg = get_settings()
    if not cfg.pyrus_enabled:
        print("Pyrus не настроен: задай PYRUS_LOGIN, PYRUS_SECURITY_KEY, PYRUS_FORM_ID в .env", file=sys.stderr)
        return 2
    engine = make_engine(cfg.database_url)
    try:
        async with PyrusClient(cfg.pyrus_login, cfg.pyrus_security_key, auth_url=cfg.pyrus_auth_url) as client:
            sync = CertificateSync(
                make_session_factory(engine), client, form_id=cfg.pyrus_form_id,
                font_path=cfg.pdf_font_path, download_days=cfg.cert_download_days,
            )
            while True:
                report = await sync.run()
                print(report)
                if not loop:
                    return 0 if not (report.push_failed or report.replies_failed or report.paper_failed or report.refresh_failed) else 1
                await asyncio.sleep(max(cfg.pyrus_sync_interval, 30))
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сверка заявок на справки с Pyrus")
    parser.add_argument("--loop", action="store_true", help="повторять раз в PYRUS_SYNC_INTERVAL секунд")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return asyncio.run(_main(args.loop))


if __name__ == "__main__":
    sys.exit(main())
