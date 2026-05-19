"""Per-user asyncio-локи на доступ к ``HHClient`` и ``session_file``.

``HHClient.__aexit__`` пишет cookies в ``session_file`` (pickle). Если
параллельно работают «основной» клиент (например, при следующем
запросе юзера) и фоновый воркер, pickle может порваться и cookies
потерятся. Сериализуем доступ.

Сейчас все потребители живут в одном процессе uvicorn'а, поэтому
``asyncio.Lock`` хватает. Когда поднимем Redis и `arq`, заменим эту
функцию на распределённый Redis-lock с тем же интерфейсом
(``async with hh_user_lock(key): ...``). Менять вызывающий код не
придётся.

В качестве ``key`` обычно передают ``hh_phone`` (он же ключ
``session_file``-а).
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


_locks: dict[str, asyncio.Lock] = {}


def _get_lock(key: str) -> asyncio.Lock:
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


@asynccontextmanager
async def user_lock(key: str) -> AsyncIterator[None]:
    """Удерживает лок по ключу на время использования ``HHClient``."""
    lock = _get_lock(key)
    async with lock:
        yield
