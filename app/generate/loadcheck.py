"""Нагрузочная проверка чтения синтетического стенда, без внешних сообщений.

    python -m app.generate.loadcheck --users 50 --rounds 3

Логин использует DEMO_PASSWORD из окружения; пароль в отчёт не попадает.
"""
import argparse
import asyncio
import json
import math
import time

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import make_engine, make_session_factory
from app.models.auth import User


async def run(base_url, users, rounds, password, database_url):
    engine = make_engine(database_url)
    try:
        async with make_session_factory(engine)() as db:
            accounts = []
            for role in ("student", "teacher", "staff"):
                accounts.extend((await db.execute(select(User.login, User.role)
                    .where(User.role == role).order_by(User.id).limit(max(1, users // 3)))).all())
            if len(accounts) < users:
                accounts.extend((await db.execute(select(User.login, User.role)
                    .where(User.login.not_in([a.login for a in accounts])).order_by(User.id)
                    .limit(users - len(accounts)))).all())
    finally:
        await engine.dispose()
    if len(accounts) < users:
        raise ValueError(f"Для {users} сессий найдено только {len(accounts)} аккаунтов")
    logins, reads, errors = [], [], []

    async def session(account):
        async with httpx.AsyncClient(base_url=base_url, timeout=60, follow_redirects=False) as client:
            began = time.perf_counter()
            response = await client.post("/login", data={"login": account.login, "password": password})
            logins.append(time.perf_counter() - began)
            if response.status_code != 303 or response.headers.get("location") == "/login":
                errors.append({"stage": "login", "status": response.status_code})
                return
            path = {"student": "/grades", "teacher": "/t/courses", "staff": "/d/home"}[account.role]
            for _ in range(rounds):
                began = time.perf_counter()
                response = await client.get(path)
                reads.append(time.perf_counter() - began)
                if response.status_code != 200:
                    errors.append({"stage": "read", "status": response.status_code})
                await asyncio.sleep(0.2)

    async def guarded(account):
        try:
            await session(account)
        except httpx.HTTPError as exc:
            errors.append({"stage": "transport", "error": type(exc).__name__})

    await asyncio.gather(*(guarded(account) for account in accounts[:users]))

    def metrics(values):
        ordered = sorted(values)
        return {"requests": len(values), "p95_seconds": round(ordered[math.ceil(len(ordered) * .95) - 1], 3)
                if ordered else None, "max_seconds": round(max(values), 3) if values else None}

    return {"sessions": users, "rounds": rounds, "login": metrics(logins), "pages": metrics(reads), "errors": errors}


def main():
    parser = argparse.ArgumentParser(description="Параллельные входы и чтение страниц вымышленного стенда")
    parser.add_argument("--base-url", help="по умолчанию PUBLIC_BASE_URL; HTTPS для Secure-cookie")
    parser.add_argument("--users", type=int, default=50)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.users <= 100 or not 1 <= args.rounds <= 20:
        parser.error("--users 1–100, --rounds 1–20")
    cfg = get_settings()
    if not cfg.demo_password:
        parser.error("Нужен DEMO_PASSWORD, только для синтетического стенда")
    report = asyncio.run(run(args.base_url or cfg.public_base_url, args.users, args.rounds,
                             cfg.demo_password, cfg.database_url))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(1 if report["errors"] else 0)


if __name__ == "__main__":
    main()
