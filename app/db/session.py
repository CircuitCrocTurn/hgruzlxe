"""Async-движок и фабрика сессий для PostgreSQL (asyncpg).

Здесь живут три вещи:

1. `engine` — глобальный async-движок SQLAlchemy поверх ``asyncpg``.
   Один на процесс. Создаётся при импорте модуля и переиспользует
   пул соединений между запросами. НЕ создавай его на каждый запрос —
   это убьёт производительность.

2. `async_session_factory` — фабрика сессий, вызывается каждый раз,
   когда нужна новая сессия. `expire_on_commit=False` — критично для
   async-кода: без него после коммита SQLAlchemy будет лениво
   подтягивать атрибуты, что в async-контексте превращается в
   неожиданные `await`-ошибки на уже-использованных объектах.

3. `get_db` — FastAPI dependency. Открывает сессию на запрос, коммитит
   при успехе, роллбэкает при исключении. Это та самая
   "transaction per request" модель — простая и предсказуемая.

Что НЕ здесь:
* Engine для Alembic — отдельный, в `alembic/env.py`. Там используется
  тот же DATABASE_URL, но с собственным циклом жизни (миграции
  должны создавать engine сами и закрывать после прогона).
* Sync-движок для воркеров — если воркеры тоже async (на asyncio
  loop), используют этот же. Если sync (Celery без gevent) — заводи
  параллельно sync-engine в отдельном модуле и не путай их.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


_settings = get_settings()


# ── Engine ───────────────────────────────────────────────────────────
#
# `pool_pre_ping=True` — перед каждой выдачей коннекта из пула делает
# дешёвый SELECT 1. Защищает от "сервер закрыл соединение, а пул не
# знает" — типичная история, когда между нами и БД стоит pgbouncer
# или сетевой балансер с idle-таймаутом.
#
# `pool_size=20, max_overflow=10` — итого 30 одновременных соединений
# с БД на процесс. Postgres по умолчанию max_connections=100, так
# что даже три инстанса API + воркер + tg-scraper укладываются.
# Если будешь масштабировать — пересмотри числа или ставь pgbouncer.
#
# `echo` — логировать все SQL-запросы в stdout. Включай ТОЛЬКО локально
# при дебаге, иначе stdout захлебнётся.
engine = create_async_engine(
    _settings.database_url,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,
    echo=True #_settings.app_debug and _settings.app_env == "development",
)


# ── Session factory ──────────────────────────────────────────────────
#
# `expire_on_commit=False` — как объяснено в docstring модуля.
# Без этого `await session.commit()` в FastAPI-эндпоинте сделает все
# атрибуты возвращаемого объекта "expired", и Pydantic при сериализации
# попытается их перечитать — а это требует await, которого в Pydantic
# нет → MissingGreenlet error.
async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


# ── FastAPI dependency ───────────────────────────────────────────────
async def get_db() -> AsyncIterator[AsyncSession]:
    """Открывает сессию на время запроса.

    Использование:
        from fastapi import Depends
        from sqlalchemy.ext.asyncio import AsyncSession
        from app.db.session import get_db

        @router.get("/users/me")
        async def me(db: AsyncSession = Depends(get_db)):
            ...

    Семантика: "одна транзакция на запрос".
        * успех эндпоинта → `commit()`
        * любое исключение → `rollback()`
        * в любом случае → `close()` (через `async with`)

    Если внутри сервиса нужна более мелкая гранулярность транзакций
    (например, "обработать 1000 вакансий, коммитя каждые 100"), не
    используй этот dependency — открывай сессии вручную через
    `async_session_factory()` и управляй транзакциями сам.
    """
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        # close() вызывается автоматически в __aexit__ контекстника


# ── Утилита для воркеров и скриптов ──────────────────────────────────
#
# В воркерах и одноразовых скриптах нет FastAPI и его DI, поэтому
# `get_db()` не подходит. Используй так:
#
#     async with session_scope() as session:
#         ...работа с session...
#
# Семантика та же: коммит при успехе, роллбэк при исключении, закрытие
# в любом случае. Это просто синоним к `async_session_factory()` с
# понятным именем — чтобы в коде воркера сразу было видно, где границы
# транзакции.
from contextlib import asynccontextmanager


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Как `get_db`, но для не-FastAPI контекстов (воркеры, скрипты, тесты)."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
