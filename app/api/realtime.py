"""WebSocket-эндпоинты ``/ws/chats`` и ``/ws/responses``: live-обновление
левой панели чатов и фида откликов соответственно.

Контракт
--------
Клиент открывает соединение, не передавая никаких заголовков кроме
куки сессии (auth-cookie выставляет ``SessionMiddleware`` после OTP).

Сервер → клиент (всегда JSON):
    /ws/chats:
        {"type": "snapshot", "chats": [...], "chat_counts": {...}, "ts": "<iso>"}
    /ws/responses:
        {"type": "snapshot", "responses": [...], "response_stats": {...}, "ts": "<iso>"}
    общие:
        {"type": "ping",     "ts": "<iso>"}
        {"type": "error",    "code": "...", "message": "..."} (перед close)

Клиент → сервер:
    Любая текстовая фрейм трактуется как «keep-alive», игнорируется.
    На практике фронт шлёт ``{"type": "pong"}``.

Жизненный цикл
--------------
1. ``websocket.session.get("user")`` → если пусто, закрываем с 4401.
2. ``ensure_user_for_session`` → если в БД нет такого юзера или он
   ``is_active=False`` — 4401 / 4403.
3. ``accept()`` → подписываемся в нужный хаб → шлём **исходный
   snapshot** (текущее состояние БД) сразу. С этой точки UI имеет
   живой список.
4. Параллельно: на каждое событие из хаба → читаем БД → шлём
   ``snapshot``. На таймауте чтения из очереди → шлём ``ping``
   (keep-alive, чтобы прокси не убил idle-соединение).
5. На ``WebSocketDisconnect`` или любое исключение —
   ``unsubscribe`` и закрываем.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.db.dashboard_data import (
    chats_status_counts,
    get_response_cards_and_stats,
    list_chat_cards,
)
from app.db.session import async_session_factory
from app.db.users import ensure_user_for_session
from app.services.realtime import (
    subscribe,
    subscribe_responses,
    unsubscribe,
    unsubscribe_responses,
)

router = APIRouter()
logger = logging.getLogger("offerday.ws")


#: Через сколько секунд молчания шлём ``ping``. Дефолтные idle-таймауты
#: у Nginx / Caddy / Cloudflare — 60s, 75s, 100s; держим запас.
_PING_INTERVAL_SECONDS = 25.0


async def _build_chats_snapshot(user_id) -> dict[str, Any]:
    """Собрать ``snapshot`` для одного юзера: список чатов + счётчики."""
    async with async_session_factory() as db:
        chats = await list_chat_cards(db, user_id=user_id)
        counts = await chats_status_counts(db, user_id=user_id)
    return {
        "type": "snapshot",
        "chats": chats,
        "chat_counts": counts,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


async def _build_responses_snapshot(user) -> dict[str, Any]:
    """Собрать ``snapshot`` страницы ``/responses`` для одного юзера.

    Возвращает ровно ту же структуру (``responses`` + ``response_stats``),
    что уже возвращает ``GET /api/responses`` — на фронте можно
    переиспользовать тот же код применения данных.
    """
    async with async_session_factory() as db:
        payload = await get_response_cards_and_stats(db, user)
    return {
        "type": "snapshot",
        "responses": payload.get("responses", []),
        "response_stats": payload.get("response_stats", {}),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


async def _resolve_user(ws: WebSocket):
    """Достать ``User`` из сессии WebSocket'а. ``None`` — закрываем."""
    session_user = ws.session.get("user") if hasattr(ws, "session") else None
    if not session_user:
        return None
    async with async_session_factory() as db:
        user = await ensure_user_for_session(db, session_user)
    if user is None or not user.is_active:
        return None
    return user


async def _run_push_loop(
    websocket: WebSocket,
    user,
    *,
    subscribe_fn: Callable[..., asyncio.Queue],
    unsubscribe_fn: Callable[..., None],
    build_snapshot: Callable[[Any], Awaitable[dict[str, Any]]],
    log_name: str,
) -> None:
    """Универсальная реализация WebSocket push-цикла.

    Делает: ``accept`` → подписка → исходный snapshot → цикл «событие
    из очереди → snapshot». Используется обоими эндпоинтами; конкретика
    (какой ``subscribe``, как строить snapshot) передаётся параметрами.

    ``build_snapshot`` принимает один аргумент: для chats — ``user.id``,
    для responses — ``User``-row (нужен сам объект, см.
    :func:`get_response_cards_and_stats`).

    Detect-disconnect
    -----------------
    На каждом тике гоним наперегонки два await'а через
    :func:`asyncio.wait` с ``return_when=FIRST_COMPLETED``:

    * ``queue.get()`` — нормальное событие из хаба → шлём snapshot;
    * ``drain_task`` (``receive_text()`` в цикле) — он завершится с
      :class:`WebSocketDisconnect` ровно в момент, когда клиент
      закрыл соединение → мы выходим из цикла **сразу**, не ждём
      следующего ping-таймаута.

    Если оба future пендингуют дольше ``_PING_INTERVAL_SECONDS`` —
    шлём keep-alive ``ping``. Любая ошибка отправки в сокет
    (``WebSocketDisconnect`` / ``RuntimeError`` от Starlette на
    «сокет уже закрыт») трактуется как «клиент ушёл» и **не**
    логируется как ERROR.
    """
    await websocket.accept()
    # ``build_snapshot`` принимает либо user_id, либо user. Передаём то,
    # что нужно конкретному builder'у (см. сигнатуры выше).
    snapshot_arg = (
        user if build_snapshot is _build_responses_snapshot else user.id
    )

    # ``RuntimeError`` от Starlette при попытке писать в уже закрытый
    # сокет — нормальный исход, не баг. Ловим вместе с
    # ``WebSocketDisconnect`` и выходим из цикла молча.
    _SEND_CLOSED = (WebSocketDisconnect, RuntimeError)

    queue = subscribe_fn(user.id)
    drain_task: asyncio.Task | None = None
    try:
        # Сразу шлём текущее состояние, чтобы UI имел из чего рисовать.
        try:
            snapshot = await build_snapshot(snapshot_arg)
            await websocket.send_json(snapshot)
        except _SEND_CLOSED:
            # Клиент успел отвалиться между accept и первым send.
            return

        # Фоновая задача: слушать входящие фреймы от клиента. Их мы
        # не используем (нет команд) — но read нужен, чтобы ловить
        # обрыв соединения. Без него обрыв детектится только при
        # следующей попытке записи, что в push-only-протоколе может
        # быть через ``_PING_INTERVAL_SECONDS``.
        async def _drain_incoming() -> None:
            while True:
                # ``receive_text`` бросит ``WebSocketDisconnect`` при close.
                await websocket.receive_text()

        drain_task = asyncio.create_task(
            _drain_incoming(), name=f"ws-{log_name}-drain-{user.id}",
        )

        while True:
            # Один экземпляр getter'а на тик. Если выйдем по таймауту
            # или по drain'у — getter отменим, и очередь НЕ потеряет
            # элемент (см. контракт ``asyncio.Queue.get`` — get_nowait
            # из get() вызывается только после успешного await).
            getter = asyncio.ensure_future(queue.get())
            try:
                done, _pending = await asyncio.wait(
                    {getter, drain_task},
                    timeout=_PING_INTERVAL_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                # Корутина целиком отменяется (shutdown / cancel
                # извне). Подбираем за собой getter и пробрасываем.
                getter.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await getter
                raise

            if drain_task in done:
                # Клиент закрыл соединение. ``drain_task.result()``
                # обычно даст ``WebSocketDisconnect``; нам это уже не
                # интересно — просто аккуратно тушим getter и выходим.
                getter.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await getter
                break

            if not done:
                # Таймаут — ничего не пришло, шлём keep-alive ping.
                getter.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await getter
                try:
                    await websocket.send_json({
                        "type": "ping",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    })
                except _SEND_CLOSED:
                    break
                continue

            # Дошли сюда — значит ``getter`` готов с событием из хаба.
            try:
                event = getter.result()
            except Exception:
                # Очередь закрылась / другая внутренняя ошибка —
                # на этом уровне делать нечего, выходим.
                break

            if event.get("type") == "closed":
                # Хаб тушится (lifespan shutdown) — будит подписчиков
                # синтетическим событием.
                break

            try:
                snapshot = await build_snapshot(snapshot_arg)
                await websocket.send_json(snapshot)
            except _SEND_CLOSED:
                break
    except WebSocketDisconnect:
        # Нормальный разрыв — клиент закрыл вкладку / ушёл со страницы.
        pass
    except Exception:
        logger.exception(
            "ws_%s failed for user=%s", log_name, user.id,
        )
    finally:
        if drain_task is not None:
            drain_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await drain_task
        unsubscribe_fn(user.id, queue)
        with suppress(Exception):
            await websocket.close()


@router.websocket("/ws/chats")
async def ws_chats(websocket: WebSocket) -> None:
    """Push-обновления левой панели чатов.

    См. docstring модуля по протоколу.
    """
    user = await _resolve_user(websocket)
    if user is None:
        # 4401 — кастомный код «не авторизован» (RFC 6455 application-level
        # range 4000–4999). Фронт по нему редиректит на /login.
        await websocket.close(code=4401)
        return

    await _run_push_loop(
        websocket, user,
        subscribe_fn=subscribe,
        unsubscribe_fn=unsubscribe,
        build_snapshot=_build_chats_snapshot,
        log_name="chats",
    )


@router.websocket("/ws/responses")
async def ws_responses(websocket: WebSocket) -> None:
    """Push-обновления фида откликов на странице ``/responses``.

    Снапшот содержит ``responses`` (полный список карточек, DESC по
    ``created_at``) и ``response_stats`` (агрегаты sent/pending/
    rejected/total для шапки) — тот же payload, что у ``GET /api/responses``.

    Когда воркер ``work_at_all`` создаёт новый ``VacancyResponse`` и
    вызывает ``notify_response_change`` — мы здесь получаем сигнал и
    пушим обновлённый снапшот. Юзеру на странице это выглядит как
    «карточка отклика появилась сверху без перезагрузки».
    """
    user = await _resolve_user(websocket)
    if user is None:
        await websocket.close(code=4401)
        return

    await _run_push_loop(
        websocket, user,
        subscribe_fn=subscribe_responses,
        unsubscribe_fn=unsubscribe_responses,
        build_snapshot=_build_responses_snapshot,
        log_name="responses",
    )
