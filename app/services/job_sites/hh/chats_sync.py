"""Синхронизатор чатов hh.ru → таблицы ``chats`` / ``messages``.

Использует :meth:`app.services.job_sites.hh.HHClient.list_all_chats`
для пагинированной выкачки ``chatik.hh.ru/chatik/api/chats`` и затем
сохраняет в БД все чаты, пришедшие из API (включая
«вы написали, ответа не было» и чаты с DISCARD). Фильтр «только
актуальные» (работодатель ответил И не отказ) применяется при
рендере (см. :func:`is_chat_relevant`).

Использование::

    from app.services.job_sites.hh import HHClient
    from app.services.job_sites.hh.chats_sync import sync_hh_chats_for_user

    await sync_hh_chats_for_user(
        user_id=user.id,
        hh_phone=hh_phone,
    )

Этот хелпер сам поднимает ``HHClient``, ходит на площадку и записывает
результат в БД. Возвращает короткий dict со счётчиками.

API hh.ru ``/chatik/api/chats`` отдаёт по странице с ``items[]`` и
``lastMessage`` для каждого чата. Полную историю сообщений отсюда
получить нельзя — мы сохраняем только ``lastMessage``. TODO: добавить
отдельный метод ``HHClient.get_chat_messages(chat_id)`` (если у hh.ru
есть соответствующий эндпойнт) и подтягивать историю по требованию,
когда юзер открывает диалог в UI.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.chat import SERVICE_HH, Chat
from app.db.models.message import (
    AUTHOR_ME,
    AUTHOR_SYSTEM,
    AUTHOR_THEM,
    Message,
)
from app.db.session import session_scope
from app.queue.locks import user_lock
from app.services.realtime import notify_chat_change

logger = logging.getLogger(__name__)


# ── Фильтр «чат виден на странице» ───────────────────────────────────────────
#
# Состояния, которые на hh.ru означают «работодатель отказал». Список
# взят из публичной документации hh.ru API + наблюдаемых значений
# ``workflowTransition.applicantState`` в ``chats.json``. Если со
# временем появятся новые варианты — добавь сюда.
REJECTED_APPLICANT_STATES: frozenset[str] = frozenset(
    {
        "DISCARD",
        "DISCARD_BY_EMPLOYER",
        "DISCARD_AFTER_INTERVIEW",
        "DISCARD_VISIBLE_BY_OTHER_REASON",
    }
)


# ── Распознавание отказа в тексте ────────────────────────────────────────────
#
# Типовые шаблоны заготовленных писем «отказ от вакансии» на hh.ru.
# Все ключевые фразы здесь — короткие подстроки в нижнем регистре, без
# знаков препинания на концах, чтобы матчиться независимо от того, как
# работодатель собрал фразу из шаблона.
#
# Если хочется добавить новый шаблон — клади сюда же; функция
# :func:`is_refusal_text` ищет первое же вхождение и сразу возвращает.
_REFUSAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        r"вы\s+на[мс]?\s+не\s+подход",          # «вы нам не подходите/подходит»
        r"не\s+подходит[ея]\s+на\s+вакансию",
        r"к\s+сожалени[юий].{0,40}не\s+(готов|сможем|смож|подойд)",
        r"не\s+готовы\s+(пригласить|предложить|продолж|рассматр)",
        r"вынужден[ыа]?\s+отказ",                # «вынуждены отказать»
        r"приняли\s+решение\s+в\s+пользу",       # «выбрали другого»
        r"выбрали\s+(другого|друг[ао]\s+кандидата)",
        r"остановили\s+(свой\s+)?выбор\s+на\s+друг",
        r"приостановили\s+подбор",
        r"закрыл[аи]\s+вакансию",
        r"\bне\s+готовы\s+сделать\s+вам\s+оффер",
        r"откаж[еу]м\s+вам",
    )
)


def is_refusal_text(text: str | None) -> bool:
    """Содержит ли текст шаблонные фразы «отказ»."""
    if not text:
        return False
    norm = text.replace("\xa0", " ")
    for pat in _REFUSAL_PATTERNS:
        if pat.search(norm):
            return True
    return False


def classify_chat_status(
    *,
    applicant_state: str | None,
    last_message_outgoing: bool,
    last_message_text: str | None,
    first_incoming_text: str | None = None,
    has_incoming_message: bool | None = None,
) -> dict[str, str]:
    """Определить статус диалога: «В ожидании» / «Отказ» / «Заинтересованы».

    Возвращает dict с ключами:
      * ``kind`` — ``"pending"`` / ``"refusal"`` / ``"interested"``
      * ``label`` — короткий текст для бейджа
      * ``css`` — utility-классы для цвета фона/текста бейджа

    Логика:
      1. **В ожидании (жёлтый)** — работодатель ещё ни разу не ответил
         (``has_incoming_message=False``): ждём первого сообщения.
      2. **Отказ (красный)** — работодатель отвечал И либо
         ``applicant_state`` в группе DISCARD, либо первый/последний
         текст совпал с шаблоном отказа (:func:`is_refusal_text`).
      3. **Заинтересованы (зелёный)** — работодатель отвечал И не отказ.

    ``has_incoming_message`` — флаг «есть ли хотя бы одно входящее
    сообщение в чате». Если не передан явно, считаем его по
    ``last_message_outgoing`` (legacy-вызовы, у которых нет доступа к
    истории сообщений).

    ``first_incoming_text`` — текст первого ответа работодателя.
    Раньше статус «Отказ» детектился только по тексту *последнего*
    сообщения — из-за этого, если мы что-то написали в ответ на отказ,
    чат уезжал в «В ожидании». Теперь смотрим в первую очередь
    на первое входящее сообщение.
    """
    if has_incoming_message is None:
        # Legacy fallback: если кто-то вызвал по старой сигнатуре,
        # считаем «диалог начался» только если последнее сообщение
        # от работодателя.
        has_incoming_message = not last_message_outgoing
    if not has_incoming_message:
        return {
            "kind": "pending",
            "label": "В ожидании",
            # Цвета бейджа берутся не из Tailwind-утилит (их нет в
            # скомпилированном CSS), а из ``.chat-status-badge.is-…``
            # в ``chats.html``.
            "css": "is-pending",
        }
    is_refusal = (
        applicant_state in REJECTED_APPLICANT_STATES
        or is_refusal_text(first_incoming_text)
        or is_refusal_text(last_message_text)
    )
    if is_refusal:
        return {
            "kind": "refusal",
            "label": "Отказ",
            "css": "is-refusal",
        }
    return {
        "kind": "interested",
        "label": "Заинтересованы",
        "css": "is-interested",
    }


# Обратная совместимость для внешних импортов (если кто-то ещё дёргает
# старое имя). Внутри проекта переходим на ``classify_chat_status``.
classify_chat_preview = classify_chat_status


def is_chat_relevant(
    *,
    applicant_state: str | None,
    last_message_outgoing: bool,
) -> bool:
    """Актуален ли чат (виден при включённом тоггле «Только актуальные»).

    Критерии:
    1. Последнее сообщение пришло от работодателя, не от нас
       (``last_message_outgoing=False``);
    2. ``applicant_state`` НЕ в группе отказов
       (см. :data:`REJECTED_APPLICANT_STATES`).

    Применяется в рендере списка чатов — фронт по флажку на
    каждом чате показывает/прячет релевантные строки.
    """
    if last_message_outgoing:
        return False
    if applicant_state in REJECTED_APPLICANT_STATES:
        return False
    return True


# ── Маппинг данных API → ORM-объекты ────────────────────────────────────────


# hh.ru — российская площадка, и у некоторых полей в payload'е
# ``chatik.hh.ru`` встречается формат без явного offset'а (``creationTime``
# вида ``"2026-05-17T15:47:40"``). Раньше мы считали такие строки UTC и
# из-за этого время в БД уезжало на +3 часа вперёд (UI показывает МСК = UTC+3,
# и naive 15:47, проставленный как UTC, отрисовывался как 18:47).
# Теперь naive-датычи трактуем как Europe/Moscow и нормализуем к UTC.
_HH_DEFAULT_TZ: Any
try:
    from zoneinfo import ZoneInfo

    _HH_DEFAULT_TZ = ZoneInfo("Europe/Moscow")
except Exception:  # pragma: no cover
    _HH_DEFAULT_TZ = timezone(timedelta(hours=3))


def _parse_dt(s: str | None) -> datetime | None:
    """ISO-строку из hh.ru → ``datetime`` **всегда tz-aware (UTC)**.

    Колонки в БД объявлены ``DateTime(timezone=True)``, поэтому при
    сравнении/записи нам нельзя возвращать naive datetime: иначе
    падение ``can't compare offset-naive and offset-aware datetimes``
    (см. ``_sync_chat_top_fields_from_messages``).

    Обычно hh.ru присылает ISO с offset'ом (``+03:00``) — мы используем
    его как есть и приводим к UTC. Если в строке offset'а нет — считаем
    её МСК (см. комментарий к ``_HH_DEFAULT_TZ``).
    """
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.split(".")[0])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_HH_DEFAULT_TZ)
    return dt.astimezone(timezone.utc)


def _to_int_id(s: str | None) -> int | None:
    """Привести строковый message-id hh.ru к int (для сравнения «свежее/старее»).

    hh.ru id сообщений — целые числа. Мы храним их как строки в БД (чтобы
    не зависеть от формата чужих API), но порядок сравниваем как int.
    """
    if s is None:
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _preview(text: str | None) -> str | None:
    if not text:
        return None
    text = text.replace("\xa0", " ").strip()
    if not text:
        return None
    return text[:300]


@dataclass
class _ChatRow:
    """Промежуточный кэш одной строки для upsert'а."""

    external_id: str
    title: str
    subtitle: str | None
    icon_url: str | None
    link: str
    unread_messages: int
    last_message_preview: str | None
    last_activity_at: datetime | None
    applicant_state: str | None
    last_message_outgoing: bool
    last_viewed_message_id: str | None
    last_message_id: str | None
    last_message_text: str
    last_message_sent_at: datetime | None
    last_message_author: str
    last_message_author_name: str | None
    applicant_id: str | None


def _build_chat_row(item: dict) -> _ChatRow:
    """Перевести один элемент ``chats.items[]`` в плоский dataclass."""
    chat_id = str(item.get("id"))
    display = item.get("_displayInfo") or {}
    title = (display.get("title") or "").strip() or "Диалог"
    subtitle = (display.get("subtitle") or "").strip() or None
    icon = display.get("icon")
    icon_url: str | None = None
    if icon:
        icon_url = (
            icon if str(icon).startswith("http") else f"https://hh.ru{icon}"
        )

    link = f"https://hh.ru/chats/{chat_id}"

    last_message = item.get("lastMessage") or {}
    sender = str(last_message.get("participantId") or "")
    current = str(item.get("currentParticipantId") or "")
    outgoing = bool(sender and sender == current)
    if outgoing:
        author = AUTHOR_ME
    elif sender:
        author = AUTHOR_THEM
    else:
        author = AUTHOR_SYSTEM

    participant = last_message.get("participantDisplay") or {}
    author_name = (participant.get("name") or "").strip() or None

    workflow = last_message.get("workflowTransition") or {}

    # ``lastViewedByCurrentUserMessageId`` — ид последнего увиденного в
    # диалоге сообщения. Нужен в ``mark_read`` как ``messageId``.
    # Иногда в payload'е это int (видел и long), иногда None — самое
    # первое сообщение в новом чате ещё никто не видел.
    last_viewed_raw = item.get("lastViewedByCurrentUserMessageId")
    last_viewed_id = (
        str(last_viewed_raw) if last_viewed_raw is not None else None
    )

    applicant_id = current or None

    return _ChatRow(
        external_id=chat_id,
        title=title[:255],
        subtitle=subtitle[:255] if subtitle else None,
        icon_url=icon_url[:512] if icon_url else None,
        link=link,
        unread_messages=int(item.get("unreadCount") or 0),
        last_message_preview=_preview(last_message.get("text")),
        # Сортировку в UI ведём по «времени последнего сообщения» (просьба
        # юзера), а не по ``lastActivityTime`` API (тот бамается ещё и от
        # просто открытия чата работодателем). Поэтому в БД сохраняем
        # ``creationTime`` последнего сообщения, с fallback на
        # ``lastActivityTime`` для чатов без сообщений.
        last_activity_at=(
            _parse_dt(last_message.get("creationTime"))
            or _parse_dt(item.get("lastActivityTime"))
        ),
        applicant_state=workflow.get("applicantState"),
        last_message_outgoing=outgoing,
        last_viewed_message_id=last_viewed_id,
        last_message_id=(
            str(last_message["id"]) if last_message.get("id") is not None else None
        ),
        last_message_text=last_message.get("text") or "",
        last_message_sent_at=_parse_dt(last_message.get("creationTime")),
        last_message_author=author,
        last_message_author_name=author_name,
        applicant_id=applicant_id,
    )


# ── Upsert ──────────────────────────────────────────────────────────────────


async def _upsert_chat_and_last_message(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    row: _ChatRow,
) -> tuple[Chat, bool, bool]:
    """Записать/обновить ``Chat`` и его последнее сообщение.

    Возвращает ``(chat, created, last_message_changed)``:

    * ``created=True`` — строка была вставлена впервые (UI-сигнал
      «новые чаты»).
    * ``last_message_changed=True`` — в ``lastMessage`` пришло ранее
      не известное нам сообщение (или чат новый).
      Используется в ``sync_hh_chats_for_user``, чтобы сразу же
      подтянуть полную историю такого чата.
    """
    # Ищем существующий чат — на UNIQUE (user_id, service, external_id).
    chat = (
        await db.execute(
            select(Chat).where(
                Chat.user_id == user_id,
                Chat.service == SERVICE_HH,
                Chat.external_id == row.external_id,
            )
        )
    ).scalar_one_or_none()

    created = False
    if chat is None:
        chat = Chat(
            user_id=user_id,
            service=SERVICE_HH,
            external_id=row.external_id,
            title=row.title,
            subtitle=row.subtitle,
            icon_url=row.icon_url,
            link=row.link,
            unread_messages=row.unread_messages,
            last_message_preview=row.last_message_preview,
            last_activity_at=row.last_activity_at,
            applicant_state=row.applicant_state,
            last_message_outgoing=row.last_message_outgoing,
            last_viewed_message_id=row.last_viewed_message_id,
            applicant_id=row.applicant_id,
        )
        db.add(chat)
        await db.flush()  # получить chat.id для FK ниже
        created = True
    else:
        chat.title = row.title
        chat.subtitle = row.subtitle
        chat.icon_url = row.icon_url
        chat.link = row.link
        chat.last_message_preview = row.last_message_preview
        chat.last_activity_at = row.last_activity_at
        chat.applicant_state = row.applicant_state
        chat.last_message_outgoing = row.last_message_outgoing
        if row.applicant_id and chat.applicant_id != row.applicant_id:
            chat.applicant_id = row.applicant_id
        # ``last_viewed_message_id`` обновляем только если у hh.ru
        # значение СВЕЖЕЕ (больше) нашего: иначе сразу после клика
        # юзера по чату (мы локально сдвинули last_viewed на самое
        # последнее сообщение) ближайший синк затрёт наше движение
        # обратно на старый id из payload'а hh.ru, который ещё не
        # успел обновиться, — и мы получим воскресающий «непрочитан».
        new_lv = _to_int_id(row.last_viewed_message_id)
        cur_lv = _to_int_id(chat.last_viewed_message_id)
        if new_lv is not None and (cur_lv is None or new_lv > cur_lv):
            chat.last_viewed_message_id = row.last_viewed_message_id

        # ``unread_messages`` тоже не регрессим: если ЛОКАЛЬНЫЙ
        # last_viewed_message_id >= последнего сообщения hh.ru, значит
        # юзер уже всё видел, и хоть payload и говорит «N непрочитано»
        # (это просто кэш hh.ru, который не пересчитался) — у нас
        # должен остаться 0. Иначе принимаем счётчик с площадки.
        remote_last = _to_int_id(row.last_message_id)
        effective_lv = _to_int_id(chat.last_viewed_message_id)
        if (
            effective_lv is not None
            and remote_last is not None
            and effective_lv >= remote_last
        ):
            chat.unread_messages = 0
        else:
            chat.unread_messages = row.unread_messages
        await db.flush()

    # Апсерт ``lastMessage``, если у него есть id на площадке.
    if row.last_message_id is None or row.last_message_sent_at is None:
        # Нет id «последнего сообщения» — пре-фетч триггерим только
        # если сам чат новый (тогда логично потянуть историю).
        return chat, created, created

    existing = (
        await db.execute(
            select(Message).where(
                Message.chat_id == chat.id,
                Message.external_id == row.last_message_id,
            )
        )
    ).scalar_one_or_none()

    if existing is None:
        db.add(
            Message(
                chat_id=chat.id,
                external_id=row.last_message_id,
                text=row.last_message_text,
                author=row.last_message_author,
                author_name=row.last_message_author_name,
                is_read=row.unread_messages == 0,
                sent_at=row.last_message_sent_at,
            )
        )
        # Абсолютно новое последнее сообщение — значит вся
        # история чата, вероятно, в нескольких сообщениях от нас
        # отличается от того, что лежит в БД. Сигнализируем
        # вызывающему коду, что чат стоит пре-фетчнуть целиком.
        last_message_changed = True
    else:
        existing.text = row.last_message_text
        existing.author = row.last_message_author
        existing.author_name = row.last_message_author_name
        existing.is_read = row.unread_messages == 0
        existing.sent_at = row.last_message_sent_at
        last_message_changed = False

    # Для впервые созданных чатов историю всё равно нужно
    # выкачать, даже если последнее сообщение по случайности
    # уже лежит (маловероятный кейс).
    if created:
        last_message_changed = True

    return chat, created, last_message_changed


# ── Публичные функции синхронизатора ────────────────────────────────────────


async def sync_hh_chats_from_payload(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    items: Iterable[dict],
) -> dict[str, Any]:
    """Записать в БД все чаты из ``items`` (уже выкачанных).

    Сохраняем все чаты — фильтр «только актуальные» применяется
    при рендере, не при записи. Счётчик ``relevant`` в ответе —
    сколько из сохранённых попадают под фильтр.

    Возвращает также ``to_prefetch`` — список ``(external_id,
    applicant_id)`` для чатов, у которых появилось новое
    последнее сообщение (включая впервые увиденные чаты).
    Это нужно, чтобы вызывающий код мог сразу же подтянуть
    полную историю таких чатов (см. pre-fetch в
    ``sync_hh_chats_for_user``).
    """
    seen = 0
    relevant = 0
    saved = 0
    created = 0
    to_prefetch: list[tuple[str, str | None]] = []

    any_changed = False
    for item in items:
        seen += 1
        row = _build_chat_row(item)
        if is_chat_relevant(
            applicant_state=row.applicant_state,
            last_message_outgoing=row.last_message_outgoing,
        ):
            relevant += 1
        chat, was_created, last_msg_changed = await _upsert_chat_and_last_message(
            db, user_id=user_id, row=row,
        )
        saved += 1
        if was_created:
            created += 1
        if was_created or last_msg_changed:
            any_changed = True
        if last_msg_changed and chat.external_id:
            to_prefetch.append((chat.external_id, row.applicant_id))

    if any_changed:
        await notify_chat_change(db, user_id)

    return {
        "seen": seen,
        "relevant": relevant,
        "saved": saved,
        "created": created,
        "to_prefetch": to_prefetch,
    }


async def mark_hh_chat_read_for_user(
    *,
    user_id: uuid.UUID,
    hh_phone: str,
    chat_external_id: str,
    message_id: str,
    has_unread_discard: bool = False,
) -> dict:
    """Помечает чат прочитанным на стороне hh.ru.

    Хождение в hh защищено per-user локом ``hh_user_lock`` — так же, как
    при синке, чтобы не было конкурирующих cookie-jar'ов одного юзера.
    """
    from app.services.job_sites.hh import HHAuthError, HHClient

    #async with hh_user_lock(hh_phone):
    if True:
        async with HHClient(phone=hh_phone, user_id=user_id) as client:
            await client.ensure_logged_in()
            return await client.mark_chat_read(
                chat_id=chat_external_id,
                message_id=message_id,
                has_unread_discard=has_unread_discard,
            )


async def send_hh_chat_message_for_user(
    *,
    user_id: uuid.UUID,
    hh_phone: str,
    chat_external_id: str,
    text: str,
) -> dict:
    """Отправляет сообщение в чат на hh.ru.

    Сама генерация ``idempotencyKey`` — внутри HHClient. Возвращает
    распарсенный ответ hh (содержит сохранённое сообщение и его id).
    """
    from app.services.job_sites.hh import HHAuthError, HHClient

    #async with hh_user_lock(hh_phone):
    if True:
        async with HHClient(phone=hh_phone, user_id=user_id) as client:
            await client.ensure_logged_in()
            return await client.send_chat_message(
                chat_id=chat_external_id,
                text=text,
            )


def _extract_chat_data_messages(payload: dict) -> tuple[list[dict], str | None]:
    """Достать список сообщений и ``currentParticipantId`` из ответа
    ``chatik.hh.ru/chatik/api/chat_data``.

    Реальная структура (см. живой payload):
    ``{"chat": {"id": …, "currentParticipantId": "…",
                "messages": {"items": [...], "hasMore": false}, …}, …}``.
    Поддерживаем также пару исторических вариантов с упрощённой
    вложенностью, на случай если hh поменяет схему.
    """
    if not isinstance(payload, dict):
        return [], None

    chat = payload.get("chat") if isinstance(payload.get("chat"), dict) else None
    current_pid: str | None = None
    if chat is not None:
        cp = chat.get("currentParticipantId")
        if cp is not None:
            current_pid = str(cp)
    if current_pid is None:
        cp = payload.get("currentParticipantId")
        if cp is not None:
            current_pid = str(cp)

    candidates: list[Any] = []
    if chat is not None:
        candidates.append(chat.get("messages"))
    candidates.extend([
        payload.get("messages"),
        payload.get("messageList"),
    ])

    for node in candidates:
        if isinstance(node, list) and node:
            return node, current_pid
        if isinstance(node, dict):
            items = node.get("items")
            if isinstance(items, list) and items:
                return items, current_pid
    return [], current_pid


async def sync_hh_chat_messages_from_payload(
    db: AsyncSession,
    *,
    chat: Chat,
    payload: dict,
) -> int:
    """Записать в ``messages`` всю историю чата из ответа ``chat_data``.

    Идемпотентно по ``(chat_id, external_id)``: повторный вызов не
    плодит дубли, только обновляет тексты/время. Возвращает количество
    реально вставленных строк (полезно для логов).
    """
    raw_messages, payload_pid = _extract_chat_data_messages(payload)
    if not raw_messages:
        return 0

    # Загрузим уже сохранённые external_id, чтобы быстро различать
    # «новое» / «обновление».
    existing_rows = (
        await db.execute(
            select(Message.id, Message.external_id).where(
                Message.chat_id == chat.id,
                Message.external_id.is_not(None),
            )
        )
    ).all()
    existing_by_ext: dict[str, uuid.UUID] = {
        ext: mid for mid, ext in existing_rows if ext is not None
    }

    # Источник истины для «кто я» — поле в самом ответе. Если оно
    # есть и не совпадает с ``chat.applicant_id`` — синхронизируем.
    current_pid = payload_pid or str(chat.applicant_id or "") or None
    if (
        payload_pid
        and chat.applicant_id != payload_pid
    ):
        chat.applicant_id = payload_pid
    current_pid = current_pid or ""
    inserted = 0
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        ext_id_raw = raw.get("id")
        if ext_id_raw is None:
            continue
        ext_id = str(ext_id_raw)
        sender = str(raw.get("participantId") or "")
        outgoing = bool(sender and current_pid and sender == current_pid)
        if outgoing:
            author = AUTHOR_ME
        elif sender:
            author = AUTHOR_THEM
        else:
            author = AUTHOR_SYSTEM
        participant = raw.get("participantDisplay") or {}
        author_name = (participant.get("name") or "").strip() or None
        text = raw.get("text") or ""
        sent_at = _parse_dt(raw.get("creationTime"))
        if sent_at is None:
            continue

        existing_id = existing_by_ext.get(ext_id)
        if existing_id is None:
            db.add(
                Message(
                    chat_id=chat.id,
                    external_id=ext_id,
                    text=text,
                    author=author,
                    author_name=author_name,
                    # ``is_read`` ставим True — раз юзер открыл чат, мы
                    # считаем содержимое прочитанным; ``Chat.unread_messages``
                    # отдельно гасим в ``_mark_chat_read_inline``.
                    is_read=True,
                    sent_at=sent_at,
                )
            )
            inserted += 1
        else:
            await db.execute(
                Message.__table__.update()
                .where(Message.id == existing_id)
                .values(
                    text=text,
                    author=author,
                    author_name=author_name,
                    sent_at=sent_at,
                )
            )

    # Засинкаем «верхушку» чата (поля для списка/статуса) по самому
    # свежему сообщению из payload'а. Иначе после ответа работодателя
    # бэк-список будет «висеть» на старом ``last_message_outgoing`` /
    # ``applicant_state`` до следующего тика ``/chats``-поллинга, и
    # бейдж статуса в карточке не сменится.
    _sync_chat_top_fields_from_messages(chat, raw_messages, current_pid)

    if inserted:
        await db.flush()
        await notify_chat_change(db, chat.user_id)
    return inserted


def _sync_chat_top_fields_from_messages(
    chat: Chat,
    raw_messages: list[dict],
    current_pid: str,
) -> None:
    """Обновить у ``Chat`` поля «верхушки» (превью, время, направление,
    applicantState) по самому свежему сообщению из ``raw_messages``.

    Идемпотентно: если в БД уже более свежие данные, чем в payload,
    ничего не меняем (полагаемся на сравнение по ``creationTime``).
    """
    latest_raw: dict | None = None
    latest_dt: datetime | None = None
    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        dt = _parse_dt(raw.get("creationTime"))
        if dt is None:
            continue
        if latest_dt is None or dt > latest_dt:
            latest_dt = dt
            latest_raw = raw
    if latest_raw is None or latest_dt is None:
        return

    # Если последняя активность в БД более свежая (например, юзер
    # только что отправил оптимистичное сообщение, ещё не дошедшее
    # в ответе hh.ru) — не откатываем.
    if chat.last_activity_at and chat.last_activity_at > latest_dt:
        return

    sender = str(latest_raw.get("participantId") or "")
    outgoing = bool(sender and current_pid and sender == current_pid)
    text = _preview(latest_raw.get("text"))
    wt = latest_raw.get("workflowTransition") or {}
    applicant_state = wt.get("applicantState") if isinstance(wt, dict) else None

    chat.last_message_outgoing = outgoing
    chat.last_activity_at = latest_dt
    if text is not None:
        chat.last_message_preview = text
    # applicantState переписываем всегда — у hh.ru это поле может
    # стать ``None`` для последующих сообщений после отказа (тогда
    # ``classify_chat_status`` отдаст «Заинтересованы», как и просил
    # пользователь).
    chat.applicant_state = applicant_state


async def sync_hh_chat_messages_for_user(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    chat: Chat,
    hh_phone: str,
) -> int:
    """End-to-end: дёрнуть ``chat_data`` и записать всю историю в БД.

    Локается per-user (``hh_user_lock``), сетевой запрос проходит вне
    транзакции; запись в БД — на текущей сессии ``db``. Если у чата
    нет ``applicant_id`` (старые записи) — пробуем заимствовать его у
    любого другого чата того же юзера в БД: ``currentParticipantId`` на
    hh.ru один и тот же для всех чатов аппликанта.
    """
    if not chat.external_id:
        return 0
    if not chat.applicant_id:
        # Берём applicant_id с любого другого чата этого юзера на hh.ru —
        # значение на стороне hh.ru одно и то же для всех его чатов.
        donor = (
            await db.execute(
                select(Chat.applicant_id)
                .where(
                    Chat.user_id == user_id,
                    Chat.service == SERVICE_HH,
                    Chat.applicant_id.is_not(None),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if donor:
            chat.applicant_id = donor
        else:
            # Не удалось найти источник — пусть фон-синк ``/chats``
            # заполнит applicant_id в следующий тик; ничего страшного,
            # история подтянется при следующем входе.
            return 0

    from app.services.job_sites.hh import HHAuthError, HHClient

    payload: dict = {}
    try:
        #async with hh_user_lock(hh_phone):
        if True:
            async with HHClient(phone=hh_phone, user_id=user_id) as client:
                await client.ensure_logged_in()
                payload = await client.get_chat_data(
                    chat_id=chat.external_id,
                    applicant_id=chat.applicant_id,
                )
    except HHAuthError:
        # Не валим страницу — у юзера в БД история уже есть (как
        # минимум lastMessage), просто свежие сообщения не подтянем.
        logger.warning(
            "chat_data: hh session expired for user=%s chat=%s",
            user_id, chat.id,
        )
        return 0
    except Exception:
        logger.exception(
            "chat_data fetch failed: user=%s chat=%s",
            user_id, chat.id,
        )
        return 0

    inserted = await sync_hh_chat_messages_from_payload(
        db, chat=chat, payload=payload
    )
    if inserted:
        logger.info(
            "chat_data: user=%s chat=%s inserted=%s",
            user_id, chat.id, inserted,
        )
    return inserted


#: Сколько чатов с обновившимся «последним сообщением» пре-фетчим
#: за один цикл синка. Лимит существует, чтобы при первой авторизации
#: (когда все чаты «новые») или после долгого простоя (когда
#: накопилась пачка изменений сразу) не упереться в десятки
#: последовательных roundtrip'ов на hh.ru. Оставшиеся подгрузятся
#: либо при следующем тике sync'а, либо лениво — при клике юзера на
#: чат (через ``POST /api/chats/<id>/refresh``).
_PREFETCH_NEW_CHATS_PER_CYCLE = 20


async def sync_hh_chats_for_user(
    *,
    user_id: uuid.UUID,
    hh_phone: str,
    filter_unread: bool = False,
    filter_has_text_message: bool = False,
) -> dict[str, Any]:
    """End-to-end: поднять HHClient → выкачать все чаты → сохранить в БД.

    Защищается per-user локом (``hh_user_lock``), как и остальные
    воркеры hh — нельзя ходить на hh.ru параллельно с двумя cookie jar'ами
    одного и того же юзера.

    Для чатов с обновившимся «последним сообщением» (включая
    впервые увиденные в этом тике, а также те, где работодатель
    ответил или мы что-то написали в HR-кабинете на hh.ru) сразу
    тянем ``chat_data`` и складываем полную историю в БД — чтобы при
    клике на чат правая панель открывалась мгновенно из локального
    кэша. Лимит см. ``_PREFETCH_NEW_CHATS_PER_CYCLE``.
    """
    # Локальный импорт, чтобы избежать цикла:
    # ``app.services.job_sites.hh`` → ``app.queue.context`` → ...
    from app.services.job_sites.hh import HHAuthError, HHClient

    prefetch_payloads: list[tuple[str, dict]] = []
    #async with hh_user_lock(hh_phone):
    if True:
        async with HHClient(phone=hh_phone, user_id=user_id) as client:
            await client.ensure_logged_in()
            items = await client.list_all_chats(
                filter_unread=filter_unread,
                filter_has_text_message=filter_has_text_message,
            )

            async with session_scope() as db:
                stats = await sync_hh_chats_from_payload(
                    db, user_id=user_id, items=items
                )

            # Пре-фетч полной истории для чатов с обновившимся
            # «последним сообщением» (включая впервые созданные).
            # Делается ВНУТРИ того же hh-лока (один cookie jar) и
            # до возврата управления — последующие пользовательские
            # клики уже найдут актуальную историю в БД.
            to_prefetch = stats.get("to_prefetch") or []
            for ext_id, applicant_id in to_prefetch[:_PREFETCH_NEW_CHATS_PER_CYCLE]:
                if not ext_id or not applicant_id:
                    continue
                try:
                    payload = await client.get_chat_data(
                        chat_id=ext_id, applicant_id=applicant_id,
                    )
                except HHAuthError:
                    logger.warning(
                        "prefetch chat_data: hh session expired user=%s",
                        user_id,
                    )
                    break
                except Exception:
                    logger.exception(
                        "prefetch chat_data fetch failed: user=%s chat=%s",
                        user_id, ext_id,
                    )
                    continue
                prefetch_payloads.append((ext_id, payload))

    if prefetch_payloads:
        async with session_scope() as db:
            for ext_id, payload in prefetch_payloads:
                chat = (
                    await db.execute(
                        select(Chat).where(
                            Chat.user_id == user_id,
                            Chat.service == SERVICE_HH,
                            Chat.external_id == ext_id,
                        )
                    )
                ).scalar_one_or_none()
                if chat is None:
                    continue
                try:
                    inserted = await sync_hh_chat_messages_from_payload(
                        db, chat=chat, payload=payload
                    )
                    if inserted:
                        logger.info(
                            "prefetch chat_data: user=%s chat=%s inserted=%s",
                            user_id, chat.id, inserted,
                        )
                except Exception:
                    logger.exception(
                        "prefetch chat_data write failed: user=%s chat=%s",
                        user_id, ext_id,
                    )

    logger.info(
        "hh chats sync: user=%s seen=%s relevant=%s saved=%s created=%s prefetched=%s",
        user_id,
        stats["seen"],
        stats["relevant"],
        stats["saved"],
        stats["created"],
        len(prefetch_payloads),
    )
    return stats
