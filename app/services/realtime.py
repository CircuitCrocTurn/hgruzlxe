"""Real-time push-обновления для чатов поверх Postgres LISTEN/NOTIFY.

Зачем
-----
Раньше ``chats.html`` каждые 10 секунд дёргал ``POST /api/chats/sync``,
чтобы перерисовать левую панель. Это:
  * греет hh.ru/habr.com сетью даже когда юзер сидит в одном чате;
  * проедает Postgres ``pg_stat_activity`` под холостые запросы;
  * выдаёт обновления только раз в 10 секунд.

WebSocket ``/ws/chats`` решает обе боли: сервер сам пушит снимок
левой панели в момент, когда что-то поменялось в БД у этого
конкретного юзера. Сигнал между процессами — Postgres LISTEN/NOTIFY,
канал ``offerday_chat_updates``, payload — JSON
``{"user_id": "<uuid>"}``.

Архитектура
-----------
* :class:`ChatHub` — singleton. Держит один долгоживущий
  ``asyncpg.Connection`` и слушает канал; локально диспатчит события
  подписчикам по ``user_id``.
* Подписчик — ``asyncio.Queue``, который кладёт каждое полученное
  событие. WebSocket-эндпоинт читает оттуда и отправляет в сокет.
* :func:`notify_chat_change` — то, что вызывают write-сайты
  (``chats_sync``, ``send_message``, ``mark_read``, бэкграунд-воркеры).
  Внутри это просто ``SELECT pg_notify(...)``, который Postgres
  доставит после ``COMMIT``. То есть LISTENер всегда видит уже
  закоммиченное состояние БД.

Идемпотентно к рестарту
-----------------------
* ``start_hub()`` идемпотентен (повторный вызов не плодит коннекты).
* Если LISTEN-коннект отвалился (сеть, рестарт PG) — задача
  сама перезапустится через ``_RECONNECT_BACKOFF_SECONDS``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import suppress
from typing import Any

import asyncpg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

logger = logging.getLogger("offerday.realtime")


# ── Конфиг ───────────────────────────────────────────────────────────

#: Имя канала Postgres NOTIFY/LISTEN. Идентификатор PostgreSQL —
#: ASCII, ≤63 символа, без кавычек. Менять только синхронно с
#: ``notify_chat_change`` и с ChatHub.
CHANNEL_CHAT_UPDATES = "offerday_chat_updates"

#: Второй канал NOTIFY — для страницы ``/responses``. Тот же контракт,
#: что и у чатов: payload — JSON ``{"user_id": "<uuid>"}``, источник —
#: воркеры авто-откликов (``respond_to_vacancies`` для каждой площадки).
CHANNEL_RESPONSE_UPDATES = "offerday_response_updates"

#: Сколько секунд ждать перед попыткой переподключиться к Postgres,
#: если listening-коннект упал. Линейный backoff (без экспоненты) —
#: достаточно для типового deploy/restart-окна.
_RECONNECT_BACKOFF_SECONDS = 3.0

#: Период health-check ping'а LISTEN-коннекта. asyncpg сам читает
#: NOTIFY в фоне, но если TCP-сессия тихо умерла (idle-таймаут
#: pgbouncer/NAT/балансера, короткий разрыв сети) — никаких событий
#: и никакой ошибки в коннект не прилетит. Без периодического
#: ``SELECT 1`` listener будет молча «висеть»: NOTIFY у фронта
#: перестают приходить, а реконнект не запускается, пока юзер не
#: перезагрузит вкладку. Интервал в ~25с с запасом меньше типового
#: idle-таймаута 60–120с.
_LISTEN_PING_INTERVAL_SECONDS = 25.0

#: Размер очереди событий на одного WebSocket-подписчика. Если фронт
#: не успевает читать — события начнут дропаться (см. ``publish``).
#: Это лучше, чем грозить OOM при «зависшем» клиенте: всё равно мы
#: пушим снимок (а не диф), и пропущенное событие — это просто
#: одна лишняя перерисовка, которую следующий пуш всё равно сделает.
_PER_SUBSCRIBER_QUEUE_LIMIT = 32


def _asyncpg_dsn() -> str:
    """``DATABASE_URL`` без SQLAlchemy-префикса ``+asyncpg`` — для
    нативного ``asyncpg.connect``.
    """
    dsn = get_settings().database_url
    return dsn.replace("postgresql+asyncpg://", "postgresql://", 1)


# ── Hub ──────────────────────────────────────────────────────────────


class _PubSubHub:
    """Generic LISTEN/NOTIFY → asyncio.Queue диспатчер на один канал.

    Используется как «движок» для двух независимых каналов: чатов и
    откликов. Каждый инстанс держит свой ``asyncpg.Connection`` с
    ``LISTEN <channel>`` и свою карту подписчиков
    ``user_id -> set[Queue]``. Логика конкретной фичи живёт снаружи,
    в WebSocket-эндпоинте: получив сигнал из очереди, он сам решает,
    что положить в snapshot.

    Параметры
    ---------
    channel : str
        Имя канала Postgres (``LISTEN <channel>``).
    name : str
        Короткое человекочитаемое имя для логов (``"chats"``,
        ``"responses"``).
    event_type : str
        Тип события, который кладётся в очередь подписчиков
        (``"chats_updated"`` / ``"responses_updated"``). Сами
        WebSocket-эндпоинты на это поле не смотрят — у них «один
        хаб = один тип события», но для логов и дебага полезно.
    """

    def __init__(self, channel: str, name: str, event_type: str) -> None:
        self._channel = channel
        self._name = name
        self._event_type = event_type
        self._subscribers: dict[str, set[asyncio.Queue]] = {}
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._lock = asyncio.Lock()

    # ── Подписка ─────────────────────────────────────────────────────

    def subscribe(self, user_id: uuid.UUID) -> asyncio.Queue:
        """Подписать текущую корутину на события юзера.

        Возвращает свежую :class:`asyncio.Queue`. На каждый NOTIFY
        для этого ``user_id`` в очередь кладётся ``dict`` с описанием
        события (``{"type": self._event_type}``).
        """
        key = str(user_id)
        queue: asyncio.Queue = asyncio.Queue(maxsize=_PER_SUBSCRIBER_QUEUE_LIMIT)
        self._subscribers.setdefault(key, set()).add(queue)
        return queue

    def unsubscribe(self, user_id: uuid.UUID, queue: asyncio.Queue) -> None:
        """Отписать очередь (вызывается при закрытии WebSocket'а)."""
        key = str(user_id)
        bucket = self._subscribers.get(key)
        if not bucket:
            return
        bucket.discard(queue)
        if not bucket:
            self._subscribers.pop(key, None)

    def active_user_ids(self) -> list[str]:
        """Список ``user_id`` (str) с хотя бы одним живым WS-подписчиком.

        Используется фоновым chats-sync-шедулером для chat-hub: ходить
        на hh/habr имеет смысл только за теми юзерами, у кого сейчас
        открыт ``/chats`` (т.е. есть кто-то, кто увидит push). Для
        response-hub аналогичной семантики не нужно — отклики пишутся
        в БД самим воркером ``work_at_all`` и эмитят NOTIFY на каждый
        INSERT.
        """
        return list(self._subscribers.keys())

    # ── Лайфсайкл ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Запустить фоновую LISTEN-задачу. Идемпотентно."""
        async with self._lock:
            if self._task is not None and not self._task.done():
                return
            self._stop_event = asyncio.Event()
            self._task = asyncio.create_task(
                self._listener_loop(self._stop_event),
                name=f"offerday-{self._name}-hub",
            )
        logger.info(
            "%s hub started: channel=%s", self._name, self._channel,
        )

    async def stop(self) -> None:
        """Остановить фоновую задачу и очистить подписчиков."""
        async with self._lock:
            task = self._task
            stop_event = self._stop_event
            self._task = None
            self._stop_event = None
        if stop_event is not None:
            stop_event.set()
        if task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(task, timeout=5)
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await task
        # Будим всех подписчиков, чтобы они корректно завершили цикл.
        for bucket in self._subscribers.values():
            for q in list(bucket):
                with suppress(asyncio.QueueFull):
                    q.put_nowait({"type": "closed"})
        self._subscribers.clear()
        logger.info("%s hub stopped", self._name)

    # ── Внутреннее ───────────────────────────────────────────────────

    async def _listener_loop(self, stop_event: asyncio.Event) -> None:
        """Бесконечный цикл: подключение к PG → LISTEN → health-check ping.

        Каждые :data:`_LISTEN_PING_INTERVAL_SECONDS` дёргаем ``SELECT 1``
        на listening-коннекте — это «keepalive», без которого тихий
        TCP-разрыв (idle-таймаут proxy/pgbouncer) не детектится и NOTIFY
        перестают доходить до WebSocket-подписчиков. При любой ошибке —
        реконнект через :data:`_RECONNECT_BACKOFF_SECONDS`. Завершение
        — по ``stop_event``.
        """
        while not stop_event.is_set():
            conn: asyncpg.Connection | None = None
            try:
                conn = await asyncpg.connect(_asyncpg_dsn())
                await conn.add_listener(self._channel, self._on_notify)
                logger.debug(
                    "%s hub LISTEN connected: channel=%s",
                    self._name, self._channel,
                )
                # asyncpg сам читает NOTIFY в фоне; нам нужно лишь
                # периодически тыкать коннект, чтобы заметить, если
                # сокет тихо умер.
                while not stop_event.is_set():
                    try:
                        await asyncio.wait_for(
                            stop_event.wait(),
                            timeout=_LISTEN_PING_INTERVAL_SECONDS,
                        )
                        # stop_event сработал — выходим из inner-loop.
                        break
                    except asyncio.TimeoutError:
                        pass
                    # Если коннект мёртв — execute бросит и попадём в
                    # внешний except → reconnect.
                    await conn.execute("SELECT 1")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "%s hub LISTEN connection failed, will retry in %ss",
                    self._name, _RECONNECT_BACKOFF_SECONDS,
                )
            finally:
                if conn is not None:
                    with suppress(Exception):
                        await conn.remove_listener(
                            self._channel, self._on_notify,
                        )
                    with suppress(Exception):
                        await conn.close()
            if stop_event.is_set():
                break
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(), timeout=_RECONNECT_BACKOFF_SECONDS,
                )

    def _on_notify(
        self,
        _conn: asyncpg.Connection,
        _pid: int,
        _channel: str,
        payload: str,
    ) -> None:
        """Колбэк, который asyncpg вызывает на каждом NOTIFY.

        Парсит payload и кладёт событие в очереди всех подписчиков
        этого ``user_id``. Колбэк синхронный (asyncpg-контракт),
        поэтому ``put_nowait`` без ``await``.
        """
        try:
            data = json.loads(payload) if payload else {}
        except (TypeError, ValueError):
            logger.warning(
                "%s hub: bad NOTIFY payload (not JSON): %r",
                self._name, payload,
            )
            return
        user_id = data.get("user_id") if isinstance(data, dict) else None
        if not isinstance(user_id, str) or not user_id:
            return
        bucket = self._subscribers.get(user_id)
        if not bucket:
            return
        event = {"type": self._event_type}
        # Лишний раз dict скопируем, чтобы подписчики не делили мутируемый объект.
        for q in list(bucket):
            try:
                q.put_nowait(dict(event))
            except asyncio.QueueFull:
                # Подписчик заклинил — дропаем событие. Снимок
                # перерисуем при следующем пушу.
                logger.debug(
                    "%s hub: subscriber queue full for user=%s, dropping event",
                    self._name, user_id,
                )


# Обратная совместимость: некоторые модули могут импортировать
# ``ChatHub`` по имени (например, тесты). Оставляем алиас на тот
# случай, если кто-то снаружи на него ссылался.
ChatHub = _PubSubHub


# Singletons — два независимых канала, две независимых ``asyncpg``-
# коннекции. Используются из ``app/main.py`` и ``app/api/realtime.py``.
_chat_hub = _PubSubHub(
    channel=CHANNEL_CHAT_UPDATES,
    name="chat",
    event_type="chats_updated",
)
_response_hub = _PubSubHub(
    channel=CHANNEL_RESPONSE_UPDATES,
    name="response",
    event_type="responses_updated",
)

# Старое имя (``_hub``) сохраняем как алиас: на случай, если кто-то
# в проекте импортировал внутреннюю переменную напрямую (например,
# тестами). Новый код использует ``_chat_hub`` явно.
_hub = _chat_hub


async def start_hub() -> None:
    """Точка входа для ``app/main.py:lifespan``.

    Поднимает оба хаба параллельно — два LISTEN-коннекта на каналы
    ``offerday_chat_updates`` и ``offerday_response_updates``.
    Падение одного не валит второй (внутри ``_listener_loop`` есть
    свой реконнект).
    """
    await asyncio.gather(_chat_hub.start(), _response_hub.start())


async def stop_hub() -> None:
    """Точка выхода для ``app/main.py:lifespan``. Тушит оба хаба."""
    await asyncio.gather(
        _chat_hub.stop(), _response_hub.stop(),
        return_exceptions=True,
    )


# ── Chat-канал: публичный API (бэккомпат) ────────────────────────────


def subscribe(user_id: uuid.UUID) -> asyncio.Queue:
    """Подписать корутину на события **чатов** юзера."""
    return _chat_hub.subscribe(user_id)


def unsubscribe(user_id: uuid.UUID, queue: asyncio.Queue) -> None:
    """Снять подписку с **чатов**."""
    _chat_hub.unsubscribe(user_id, queue)


def active_user_ids() -> list[str]:
    """Список ``user_id`` (str) с активным ``/ws/chats`` подписчиком.

    Безопасно дёргать из любых корутин в этом процессе — возвращается
    копия ключей словаря.
    """
    return _chat_hub.active_user_ids()


# ── Response-канал: публичный API ────────────────────────────────────


def subscribe_responses(user_id: uuid.UUID) -> asyncio.Queue:
    """Подписать корутину на события **откликов** юзера."""
    return _response_hub.subscribe(user_id)


def unsubscribe_responses(user_id: uuid.UUID, queue: asyncio.Queue) -> None:
    """Снять подписку с **откликов**."""
    _response_hub.unsubscribe(user_id, queue)


def active_response_user_ids() -> list[str]:
    """Список ``user_id`` (str) с активным ``/ws/responses`` подписчиком.

    Сейчас наружу не используется (response-hub не нуждается в
    шедулере), но симметрично с chat-hub — пригодится для метрик
    или будущего фолбэка.
    """
    return _response_hub.active_user_ids()


# ── NOTIFY-эмиттеры ──────────────────────────────────────────────────


async def _emit_notify(
    db: AsyncSession,
    *,
    channel: str,
    user_id: uuid.UUID | str,
    extra: dict[str, Any] | None,
    label: str,
) -> None:
    """Общая внутренняя реализация для двух ``notify_*_change``.

    ``label`` — что писать в логе ошибки (``"notify_chat_change"`` /
    ``"notify_response_change"``).
    """
    payload: dict[str, Any] = {"user_id": str(user_id)}
    if extra:
        payload.update(extra)
    try:
        await db.execute(
            text("SELECT pg_notify(:channel, :payload)"),
            {
                "channel": channel,
                "payload": json.dumps(payload, ensure_ascii=False),
            },
        )
    except Exception:
        # Никогда не валим основную операцию из-за NOTIFY: даже если
        # уведомление не уйдёт, БД-запись осталась корректной.
        # Фронт всё равно получит свежие данные при перезагрузке
        # страницы или при следующем event.
        logger.exception("%s failed for user=%s", label, user_id)


async def notify_chat_change(
    db: AsyncSession,
    user_id: uuid.UUID | str,
    **extra: Any,
) -> None:
    """Опубликовать «у юзера ``user_id`` что-то поменялось в чатах».

    Под капотом — ``SELECT pg_notify(:channel, :payload)`` через
    переданную сессию. Postgres доставит событие LISTENеру **после**
    того, как эта сессия закоммитится — значит, к моменту, когда
    WebSocket-эндпоинт получит сигнал и пойдёт читать БД, изменения
    уже видны другим сессиям.

    Вызывать стоит из всех write-сайтов, которые меняют ``chats`` или
    ``messages`` (см. ``app/api/dashboard.py`` и
    ``app/services/job_sites/<service>/chats_sync.py``).

    ``extra`` зарезервирован для будущих полей события (chat_id,
    причина изменения и т.п.). Сейчас игнорируется приёмником.
    """
    await _emit_notify(
        db,
        channel=CHANNEL_CHAT_UPDATES,
        user_id=user_id,
        extra=extra or None,
        label="notify_chat_change",
    )


async def notify_response_change(
    db: AsyncSession,
    user_id: uuid.UUID | str,
    **extra: Any,
) -> None:
    """Опубликовать «у юзера ``user_id`` появился/изменился отклик».

    Тот же контракт, что у :func:`notify_chat_change`, но для канала
    ``CHANNEL_RESPONSE_UPDATES``. Вызывать **после** успешного
    ``db.add(VacancyResponse)`` / ``db.flush()`` — Postgres доставит
    NOTIFY уже после ``COMMIT``, так что LISTENер увидит свежий row.

    Write-сайты:
      * ``app/services/job_sites/hh/__init__.py``  (``HHClient.respond_to_vacancies``)
      * ``app/services/job_sites/habr/__init__.py`` (``HabrClient.respond_to_vacancies``)
    """
    await _emit_notify(
        db,
        channel=CHANNEL_RESPONSE_UPDATES,
        user_id=user_id,
        extra=extra or None,
        label="notify_response_change",
    )


__all__ = (
    "CHANNEL_CHAT_UPDATES",
    "CHANNEL_RESPONSE_UPDATES",
    "active_response_user_ids",
    "active_user_ids",
    "notify_chat_change",
    "notify_response_change",
    "start_hub",
    "stop_hub",
    "subscribe",
    "subscribe_responses",
    "unsubscribe",
    "unsubscribe_responses",
)
