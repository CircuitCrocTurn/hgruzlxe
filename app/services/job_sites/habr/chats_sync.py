"""Синхронизатор чатов career.habr.com → таблицы ``chats`` / ``messages``.

Habr держит чаты в Vue/Nuxt SPA. Источники данных:

* **Список диалогов** — HTML-страница ``career.habr.com/conversations``
  с встроенным SSR-payload'ом ``<script id="__NUXT_DATA__">…</script>``
  в формате *devalue* (плоский массив с числовыми ссылками внутри).
  Карточка диалога несёт ``login`` партнёра, имя/аватар/компанию,
  ``lastMessage`` (с ``id`` и ``createdAt``), ``unreadMessagesCount``
  и ``subject`` (вакансия + ссылка).
* **История сообщений** — JSON-API
  ``GET /api/frontend_v1/chat/messages?login=&page=`` с пагинацией
  по 20 сообщений и сортировкой ASC (свежее — снизу).
* **Mark read / отправка** — POST в ``frontend_v1`` с одноразовым
  ``x-csrf-token`` из ``/api/frontend_v1/users/authenticity_token``.

Сетевая часть инкапсулирована в :class:`HabrClient`, здесь — только
парсинг и запись в БД, по такому же контракту, как
:mod:`app.services.job_sites.hh.chats_sync` (см. соседний модуль).

Использование::

    from app.services.job_sites.habr.chats_sync import (
        sync_habr_chats_for_user,
    )
    stats = await sync_habr_chats_for_user(user_id=user.id)
"""
from __future__ import annotations

import html as html_mod
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.chat import SERVICE_HABR, Chat
from app.db.models.message import (
    AUTHOR_ME,
    AUTHOR_SYSTEM,
    AUTHOR_THEM,
    Message,
)
from app.db.session import session_scope
from app.services.job_sites.hh.chats_sync import (
    is_refusal_text,
    REJECTED_APPLICANT_STATES,
)
from app.services.realtime import notify_chat_change

logger = logging.getLogger(__name__)


# ── devalue-парсер Nuxt SSR payload ─────────────────────────────────────────
#
# Формат: ``__NUXT_DATA__`` — JSON-массив, где каждый элемент это
# либо литерал (string/number/bool/null), либо ссылка по индексу
# на другой элемент. Объекты/массивы выражаются как dict/list, в
# которых *значения* — это индексы. Отсюда «дедупликация» одинаковых
# подстрок и объектов в одном массиве.
#
# Спец-маркеры (видим в живом payload'е habr):
#   ["ShallowReactive", idx], ["Reactive", idx] — пробрасываем idx;
#   ["Ref", value] — разворачиваем value;
#   ["EmptyRef", "false"|"true"] — литерал (изначально пустой ref);
#   ["Set", idx] — Set значений (нам тут не интересен, отдаём list);
#   ["NuxtError", idx], ["RouteLocation", idx], … — пробрасываем idx.
#
# Если встретили незнакомый маркер — возвращаем dict с пометкой ``_tag``,
# чтобы не терять данные. На реальном habr-payload'е достаточно набора
# выше; см. ``_test_devalue_on_html_snapshots.py`` в репо для прогона
# на трёх вложенных HTML-снимках.

_NUXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>',
    re.DOTALL,
)


class _Devalue:
    """Стейтфул-разворачиватель Nuxt SSR payload.

    Создаётся один на HTML; ``resolve(idx)`` возвращает развёрнутый
    Python-объект с мемоизацией по индексу (защита от циклов и от
    повторного дёргания одних и тех же узлов).
    """

    def __init__(self, data: list[Any]) -> None:
        self._data = data
        self._memo: dict[int, Any] = {}
        # Простая защита от циклов (если они вдруг появятся): метим
        # узел "in progress" и выходим, если попали в него повторно.
        self._in_progress: set[int] = set()

    @classmethod
    def from_html(cls, html: str) -> "_Devalue":
        m = _NUXT_DATA_RE.search(html)
        if not m:
            raise ValueError("__NUXT_DATA__ script tag не найден в HTML")
        raw = html_mod.unescape(m.group(1))
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError(
                f"__NUXT_DATA__ payload — не list (тип {type(data).__name__})"
            )
        return cls(data)

    def resolve(self, idx: Any) -> Any:
        if not isinstance(idx, int):
            return idx
        if idx < 0 or idx >= len(self._data):
            return None
        if idx in self._memo:
            return self._memo[idx]
        if idx in self._in_progress:
            return None
        self._in_progress.add(idx)
        try:
            out = self._resolve_node(self._data[idx])
        finally:
            self._in_progress.discard(idx)
        self._memo[idx] = out
        return out

    def _resolve_node(self, node: Any) -> Any:
        if isinstance(node, list):
            if node and isinstance(node[0], str):
                tag = node[0]
                if tag in (
                    "ShallowReactive",
                    "Reactive",
                    "NuxtError",
                    "RouteLocation",
                ):
                    return self.resolve(node[1]) if len(node) > 1 else None
                if tag == "Ref":
                    return self.resolve(node[1]) if len(node) > 1 else None
                if tag == "EmptyRef":
                    raw = node[1] if len(node) > 1 else None
                    if isinstance(raw, str):
                        try:
                            return json.loads(raw)
                        except (TypeError, ValueError):
                            return raw
                    return raw
                if tag == "Set":
                    if len(node) > 1:
                        inner = self.resolve(node[1])
                        return list(inner) if isinstance(inner, list) else []
                    return []
                # Незнакомый маркер — отдаём как dict, чтобы не терять данные.
                return {
                    "_tag": tag,
                    "_args": [self.resolve(x) for x in node[1:]],
                }
            return [self.resolve(x) for x in node]
        if isinstance(node, dict):
            return {k: self.resolve(v) for k, v in node.items()}
        return node

    @property
    def root(self) -> Any:
        return self.resolve(0)


def extract_conversations_from_html(
    html: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Достать список карточек диалогов и пагинацию из HTML
    ``/conversations``.

    Возвращает ``(conversations, meta)``. Если payload не найден или
    структура другая — пустые ``[]``, ``{}`` (caller сам решит, что
    делать). Не падаем — habr иногда добавляет/убирает поля и
    переключает шаблоны, лучше отдать пусто, чем сломать UI.
    """
    try:
        payload = _Devalue.from_html(html)
        root = payload.root
    except Exception as exc:  # noqa: BLE001
        logger.warning("habr: не удалось разобрать __NUXT_DATA__: %s", exc)
        return [], {}

    if not isinstance(root, dict):
        return [], {}
    data = root.get("data") or {}
    if not isinstance(data, dict):
        return [], {}

    # Vue-страница /conversations кладёт состояние под одним из двух
    # ключей: с выбранным диалогом (``…WithSelectedLoginConversation``)
    # и без. Берём первый непустой.
    block = (
        data.get("conversationsListWithSelectedLoginConversation")
        or data.get("conversationsList")
        or {}
    )
    if not isinstance(block, dict):
        return [], {}

    conversations = block.get("conversations")
    meta = block.get("meta") or {}
    if not isinstance(conversations, list):
        conversations = []
    if not isinstance(meta, dict):
        meta = {}
    return conversations, meta


# ── Маппинг payload'а habr → ORM-объекты ────────────────────────────────────


_HABR_TZ = timezone.utc

# Хабр шлёт ``createdAt`` без явного offset'а, а уже в UTC
# (наблюдается на ``career.habr.com/api/frontend_v1/...``). Раньше мы
# считали такие строки за MSK и сдвигали ещё на -3 часа — из-за этого в
# UI время «отставало» на 3 часа от настоящего МСК. Теперь naive-строки
# трактуем как UTC.


def _parse_habr_dt(s: str | None) -> datetime | None:
    """``"2026-05-17 12:47:40"`` или ISO → tz-aware UTC.

    Хабр у себя отдаёт время в UTC (без offset'а). ``fromisoformat``
    вернёт naive datetime — мы явно ставим tz=UTC, чтобы потом
    ``_to_msk`` корректно показал МСК (+3 часа).
    """
    if not s:
        return None
    s = s.strip()
    if not s:
        return None
    # Сначала пробуем ISO с offset'ом (на случай, если habr поправит API).
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Падаем в habr-специфичный формат «YYYY-MM-DD HH:MM:SS».
        try:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_HABR_TZ)
    return dt.astimezone(_HABR_TZ)


_BLOCK_TAGS_RE = re.compile(
    r"</?(p|div|li|tr|h[1-6]|blockquote|pre)[^>]*>",
    re.IGNORECASE,
)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_A_TAG_RE = re.compile(
    r'<a\b[^>]*?href=["\']([^"\']*)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_plain(html_body: str | None) -> str:
    """Habr хранит ``body`` как HTML (``<p>``, ``<a href>``, ``<br>``…).

    В таблицу ``messages`` мы кладём plain-text (см. README, секция
    «Чаты»), потому что Jinja и js-рендер на чатах включают
    HTML-escape — иначе на странице видны буквальные ``<p>`` / ``<a>``.

    Что делаем:
      * ``<a href="X">Y</a>`` → ``"Y (X)"`` (или просто ``"Y"``, если
        ссылка совпадает с текстом);
      * ``<br>`` и блочные теги (``<p>``, ``<div>``, ``<li>``, …) →
        перевод строки;
      * остальные теги — выкидываем;
      * декодируем HTML-сущности (``&amp;`` → ``&``);
      * схлопываем лишние пробелы и пустые строки.
    """
    if not html_body:
        return ""
    s = html_body

    def _link_repl(match: re.Match[str]) -> str:
        href = (match.group(1) or "").strip()
        inner = _ANY_TAG_RE.sub("", match.group(2) or "").strip()
        if not inner:
            return href
        if href and href != inner:
            return f"{inner} ({href})"
        return inner

    s = _A_TAG_RE.sub(_link_repl, s)
    s = _BR_RE.sub("\n", s)
    s = _BLOCK_TAGS_RE.sub("\n", s)
    s = _ANY_TAG_RE.sub("", s)
    s = html_mod.unescape(s)
    s = s.replace("\xa0", " ").replace("\r", "")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in s.split("\n")]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def _preview(text: str | None) -> str | None:
    if not text:
        return None
    # habr хранит body как HTML; для превью убираем теги, лишние пробелы.
    norm = _html_to_plain(text)
    norm = norm.replace("\n", " ")
    norm = re.sub(r"\s+", " ", norm).strip()
    if not norm:
        return None
    return norm[:300]


def _to_int_id(s: str | None) -> int | None:
    if s is None:
        return None
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


_HABR_BASE = "https://career.habr.com"


def _abs_url(url: str | None) -> str | None:
    """Habr иногда отдаёт относительные пути — приводим к абсолютным."""
    if not url:
        return None
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return _HABR_BASE + url
    return url


@dataclass
class _ChatRow:
    """Промежуточный кэш одной строки чата для upsert'а."""

    external_id: str         # login партнёра на habr
    title: str               # «Вакансия компании …» (или fullName, как fallback)
    subtitle: str | None     # companyName
    icon_url: str | None     # companyLogoUrl
    link: str                # https://career.habr.com/conversations/{login}
    unread_messages: int
    last_message_preview: str | None
    last_activity_at: datetime | None
    last_message_outgoing: bool
    last_message_id: str | None
    last_message_text: str
    last_message_sent_at: datetime | None
    last_message_author: str
    last_message_author_name: str | None


def _build_chat_row(item: dict, *, current_user_alias: str | None) -> _ChatRow:
    """Перевести одну карточку из ``__NUXT_DATA__`` в плоский dataclass.

    ``current_user_alias`` — наш ``alias`` на habr (см.
    ``/api/frontend_v1/users/me``). Используется, чтобы помечать
    исходящие сообщения как ``AUTHOR_ME`` (``isMine: true`` дублирует
    это, но в HAR оба поля совпадают, оставим оба источника на
    случай рассинхрона).
    """
    login = (item.get("login") or "").strip()
    full_name = (item.get("fullName") or "").strip()
    company_name = (item.get("companyName") or "").strip() or None
    company_logo = _abs_url(item.get("companyLogoUrl"))

    # subject — вакансия, по которой идёт диалог: ``{text, href}``.
    subject = item.get("subject") if isinstance(item.get("subject"), dict) else {}
    subject_text = ((subject or {}).get("text") or "").strip()

    # Заголовок карточки: в hh это «название вакансии», у habr
    # subject.text как раз даёт «Вакансия компании X. Y» — кладём
    # его. Если subject пуст (бывает у системных диалогов habr-staff)
    # — fallback на имя контакта.
    if subject_text:
        title = subject_text
    elif full_name:
        title = full_name
    else:
        title = "Диалог"

    link = f"{_HABR_BASE}/conversations/{login}" if login else _HABR_BASE

    last_message = item.get("lastMessage") or {}
    last_msg_id_raw = last_message.get("id")
    last_msg_id = str(last_msg_id_raw) if last_msg_id_raw is not None else None
    last_msg_body = _html_to_plain(last_message.get("body"))
    last_msg_dt = _parse_habr_dt(last_message.get("createdAt"))

    is_mine_flag = bool(last_message.get("isMine"))
    author_login = (last_message.get("authorLogin") or "").strip()
    outgoing = is_mine_flag or (
        bool(current_user_alias)
        and author_login == current_user_alias
    )
    kind = (last_message.get("kind") or "").strip() or "message"

    if outgoing:
        author = AUTHOR_ME
        author_name = None
    elif kind != "message":
        # «invite»/«offer»/системные — без человекоавторов.
        author = AUTHOR_SYSTEM
        author_name = None
    else:
        author = AUTHOR_THEM
        author_name = full_name or None

    unread = int(item.get("unreadMessagesCount") or 0)

    return _ChatRow(
        external_id=login,
        title=title[:255],
        subtitle=company_name[:255] if company_name else None,
        icon_url=company_logo[:512] if company_logo else None,
        link=link,
        unread_messages=unread,
        last_message_preview=_preview(last_msg_body),
        last_activity_at=last_msg_dt,
        last_message_outgoing=outgoing,
        last_message_id=last_msg_id,
        last_message_text=last_msg_body,
        last_message_sent_at=last_msg_dt,
        last_message_author=author,
        last_message_author_name=author_name,
    )


# ── Upsert ──────────────────────────────────────────────────────────────────


async def _upsert_chat_and_last_message(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    row: _ChatRow,
) -> tuple[Chat, bool, bool]:
    """Запись/обновление ``Chat`` + ``lastMessage`` для habr.

    Возвращает ``(chat, created, last_message_changed)``: вторым
    флагом сигнализируем caller'у, что историю стоит пре-фетчнуть.
    Логика повторяет соседний :mod:`...hh.chats_sync`, в том числе
    защиту от «регрессии» ``unread_messages`` (если юзер локально
    уже всё прочитал, но habr этого ещё не знает).
    """
    if not row.external_id:
        # без login'а нет UNIQUE-ключа — пропускаем; в реальной выборке
        # такого не бывает, но защищаемся от пустых dict'ов в payload'е.
        raise ValueError("habr chat without external_id (login)")

    chat = (
        await db.execute(
            select(Chat).where(
                Chat.user_id == user_id,
                Chat.service == SERVICE_HABR,
                Chat.external_id == row.external_id,
            )
        )
    ).scalar_one_or_none()

    created = False
    if chat is None:
        chat = Chat(
            user_id=user_id,
            service=SERVICE_HABR,
            external_id=row.external_id,
            title=row.title,
            subtitle=row.subtitle,
            icon_url=row.icon_url,
            link=row.link,
            unread_messages=row.unread_messages,
            last_message_preview=row.last_message_preview,
            last_activity_at=row.last_activity_at,
            # ``applicant_state`` у habr нет — статус «отказ»
            # определяем по тексту (``is_refusal_text``).
            applicant_state=None,
            last_message_outgoing=row.last_message_outgoing,
            last_viewed_message_id=None,
            applicant_id=None,
        )
        db.add(chat)
        await db.flush()
        created = True
    else:
        chat.title = row.title
        chat.subtitle = row.subtitle
        chat.icon_url = row.icon_url
        chat.link = row.link
        chat.last_message_preview = row.last_message_preview
        chat.last_activity_at = row.last_activity_at
        chat.last_message_outgoing = row.last_message_outgoing

        # Те же защиты от регрессии счётчика непрочитанных, что и в
        # hh-синке: если локально юзер уже видел сообщение свежее,
        # чем то, что прислал habr, не воскрешаем «непрочитан».
        remote_last = _to_int_id(row.last_message_id)
        cur_lv = _to_int_id(chat.last_viewed_message_id)
        if (
            cur_lv is not None
            and remote_last is not None
            and cur_lv >= remote_last
        ):
            chat.unread_messages = 0
        else:
            chat.unread_messages = row.unread_messages
        await db.flush()

    if row.last_message_id is None or row.last_message_sent_at is None:
        # У habr lastMessage всегда есть, если конверсация не пуста, но
        # на всякий случай: пре-фетч триггерим только для новых чатов.
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
        last_message_changed = True
    else:
        existing.text = row.last_message_text
        existing.author = row.last_message_author
        existing.author_name = row.last_message_author_name
        existing.is_read = row.unread_messages == 0
        existing.sent_at = row.last_message_sent_at
        last_message_changed = False

    if created:
        last_message_changed = True

    return chat, created, last_message_changed


# ── Запись истории сообщений ────────────────────────────────────────────────


def _build_message_payload(
    raw: dict, *, current_user_alias: str | None,
) -> dict[str, Any] | None:
    """Один элемент ``messages[]`` → kwargs для :class:`Message`.

    Возвращает None, если запись невалидна (нет id или времени).
    """
    if not isinstance(raw, dict):
        return None
    ext_id_raw = raw.get("id")
    if ext_id_raw is None:
        return None
    sent_at = _parse_habr_dt(raw.get("createdAt"))
    if sent_at is None:
        return None

    is_mine_flag = bool(raw.get("isMine"))
    author_login = (raw.get("authorLogin") or "").strip()
    outgoing = is_mine_flag or (
        bool(current_user_alias)
        and author_login == current_user_alias
    )
    kind = (raw.get("kind") or "").strip() or "message"

    if outgoing:
        author = AUTHOR_ME
        author_name = None
    elif kind != "message":
        author = AUTHOR_SYSTEM
        author_name = None
    else:
        author = AUTHOR_THEM
        author_name = author_login or None  # реальное имя есть только в card'е

    return {
        "external_id": str(ext_id_raw),
        # habr отдаёт ``body`` как HTML; в таблицу ``messages`` кладём
        # plain-text (см. README, секция «Чаты»), иначе на странице
        # Jinja-escape показал бы буквальные ``<p>``/``<a>``.
        "text": _html_to_plain(raw.get("body")),
        "author": author,
        "author_name": author_name,
        # ``read`` приходит true/false; для отрисовки мы и так гасим
        # счётчик при открытии (см. ``_mark_chat_read_inline``), поэтому
        # просто отражаем как есть.
        "is_read": bool(raw.get("read", True)),
        "sent_at": sent_at,
    }


async def sync_habr_chat_messages_from_payload(
    db: AsyncSession,
    *,
    chat: Chat,
    payload: dict,
    current_user_alias: str | None,
) -> int:
    """Записать в ``messages`` всю историю из ответа
    ``GET /api/frontend_v1/chat/messages``.

    Идемпотентно по ``(chat_id, external_id)``. Возвращает
    количество впервые вставленных строк. Параллельно обновляет
    «верхушку» чата (последнее сообщение, время, направление) по
    самому свежему элементу — иначе после отдельного клика на
    диалог карточка в списке могла бы «висеть» на старом превью.
    """
    raw_messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(raw_messages, list) or not raw_messages:
        return 0

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

    inserted = 0
    parsed: list[dict[str, Any]] = []
    for raw in raw_messages:
        m = _build_message_payload(raw, current_user_alias=current_user_alias)
        if m is None:
            continue
        parsed.append(m)
        ext_id = m["external_id"]
        existing_id = existing_by_ext.get(ext_id)
        if existing_id is None:
            db.add(
                Message(
                    chat_id=chat.id,
                    external_id=ext_id,
                    text=m["text"],
                    author=m["author"],
                    author_name=m["author_name"],
                    is_read=m["is_read"],
                    sent_at=m["sent_at"],
                )
            )
            inserted += 1
        else:
            await db.execute(
                Message.__table__.update()
                .where(Message.id == existing_id)
                .values(
                    text=m["text"],
                    author=m["author"],
                    author_name=m["author_name"],
                    sent_at=m["sent_at"],
                )
            )

    _sync_chat_top_fields_from_messages(chat, parsed)

    if inserted:
        await db.flush()
        await notify_chat_change(db, chat.user_id)
    return inserted


def _sync_chat_top_fields_from_messages(
    chat: Chat, parsed: list[dict[str, Any]],
) -> None:
    """Обновить ``chat.last_*`` поля по самому свежему сообщению.

    Идемпотентно: если локально уже более свежие данные (например,
    юзер прямо сейчас отправил оптимистичное сообщение, ещё не
    дошедшее в ответ habr), не откатываем.
    """
    if not parsed:
        return
    latest = max(parsed, key=lambda m: m["sent_at"])
    if chat.last_activity_at and chat.last_activity_at > latest["sent_at"]:
        return
    chat.last_activity_at = latest["sent_at"]
    chat.last_message_outgoing = latest["author"] == AUTHOR_ME
    preview = _preview(latest["text"])
    if preview is not None:
        chat.last_message_preview = preview


# ── End-to-end функции для роутов ───────────────────────────────────────────


async def sync_habr_chat_messages_for_user(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    chat: Chat,
    habr_email: str = "",
) -> int:
    """Дёрнуть ``/chat/messages?login=…`` и записать всю историю.

    ``habr_email`` нужен только для конструктора :class:`HabrClient`
    (там он используется для in-memory OTP-бриджа при первичном
    логине). У залогиненного юзера cookies подтягиваются из БД по
    ``user_id``, и email фактически не используется — пустая
    строка ок.

    Сетевые ошибки не пробрасываем — best-effort: в БД у юзера уже
    лежит ``lastMessage``, новый sync догонит при следующем кошении.
    """
    if not chat.external_id:
        return 0

    from app.services.job_sites.habr import HabrAuthError, HabrClient

    payload: dict = {}
    alias: str | None = None
    try:
        async with HabrClient(email=habr_email, user_id=user_id) as client:
            me = await client.fetch_users_me()
            user_obj = me.get("user") if isinstance(me, dict) else None
            if isinstance(user_obj, dict):
                a = user_obj.get("alias")
                if isinstance(a, str) and a:
                    alias = a
            payload = await client.fetch_chat_messages_page(
                login=chat.external_id, page=1,
            )
            # Если страниц больше одной — листаем дальше. У habr
            # обычно 20/стр, но мы и так редко превышаем; ставим
            # умеренный лимит, чтобы не зависнуть на огромных чатах.
            meta = payload.get("meta") if isinstance(payload, dict) else None
            total_pages = (
                int(meta["totalPages"])
                if isinstance(meta, dict) and isinstance(meta.get("totalPages"), int)
                else 1
            )
            for page in range(2, min(total_pages, 10) + 1):
                more = await client.fetch_chat_messages_page(
                    login=chat.external_id, page=page,
                )
                msgs = more.get("messages") if isinstance(more, dict) else None
                if isinstance(msgs, list) and msgs:
                    payload.setdefault("messages", []).extend(msgs)
    except HabrAuthError:
        logger.warning(
            "habr chat_messages: сессия протухла (user=%s chat=%s)",
            user_id, chat.id,
        )
        return 0
    except Exception:
        logger.exception(
            "habr chat_messages fetch failed: user=%s chat=%s",
            user_id, chat.id,
        )
        return 0

    inserted = await sync_habr_chat_messages_from_payload(
        db, chat=chat, payload=payload, current_user_alias=alias,
    )
    if inserted:
        logger.info(
            "habr chat_messages: user=%s chat=%s inserted=%s",
            user_id, chat.id, inserted,
        )
    return inserted


async def mark_habr_chat_read_for_user(
    *,
    user_id: uuid.UUID,
    chat_external_id: str,
    habr_email: str = "",
) -> dict:
    """Помечает диалог прочитанным на стороне habr.

    Возвращает распарсенный ответ ``{"hasBeenRead": true}``.
    Не глотает ошибки — caller сам решает (см. /api/chats/{uuid}/read).
    """
    from app.services.job_sites.habr import HabrAuthError, HabrClient

    if not chat_external_id:
        raise ValueError("mark_habr_chat_read_for_user: пустой chat_external_id")

    async with HabrClient(email=habr_email, user_id=user_id) as client:
        # is_authenticated делает GET /profile/personal/edit — лишний
        # roundtrip; пропускаем, ошибки от toggle_read_state и так
        # ловятся как HabrAuthError.
        try:
            return await client.toggle_chat_read_state(
                login=chat_external_id, new_state="read",
            )
        except HabrAuthError:
            raise
        except Exception:
            logger.exception(
                "habr mark_read failed: user=%s login=%s",
                user_id, chat_external_id,
            )
            raise


async def send_habr_chat_message_for_user(
    *,
    user_id: uuid.UUID,
    chat_external_id: str,
    text: str,
    habr_email: str = "",
) -> tuple[dict, str | None]:
    """Шлёт сообщение в habr-диалог и возвращает ``(response, alias)``.

    ``alias`` — наш собственный логин на habr (для корректной разметки
    исходящих сообщений в ``sync_habr_chat_messages_from_payload``).
    Получаем его в этом же ``HabrClient``-сеансе, чтобы не дёргать
    ``/users/me`` отдельным заходом.
    """
    from app.services.job_sites.habr import HabrAuthError, HabrClient

    if not chat_external_id:
        raise ValueError("send_habr_chat_message_for_user: пустой chat_external_id")
    if not text or not text.strip():
        raise ValueError("send_habr_chat_message_for_user: пустой текст")

    async with HabrClient(email=habr_email, user_id=user_id) as client:
        try:
            me = await client.fetch_users_me()
        except HabrAuthError:
            raise
        alias: str | None = None
        user_obj = me.get("user") if isinstance(me, dict) else None
        if isinstance(user_obj, dict):
            a = user_obj.get("alias")
            if isinstance(a, str) and a:
                alias = a

        response = await client.send_chat_message(
            login=chat_external_id, body=text,
        )
        return response, alias


# ── Список чатов ────────────────────────────────────────────────────────────


async def sync_habr_chats_from_payload(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    items: Iterable[dict],
    current_user_alias: str | None,
) -> dict[str, Any]:
    """Записать пачку карточек ``conversations[]`` в БД.

    Возвращает счётчики + ``to_prefetch`` — список ``external_id``
    чатов, у которых появилось новое последнее сообщение (или которые
    впервые увидены). Caller использует его, чтобы тут же подтянуть
    полную историю для UI.
    """
    seen = 0
    relevant = 0
    saved = 0
    created = 0
    to_prefetch: list[str] = []

    any_changed = False
    for item in items:
        seen += 1
        if not isinstance(item, dict):
            continue
        row = _build_chat_row(item, current_user_alias=current_user_alias)
        if not row.external_id:
            continue
        # «Актуальные» (зелёный бейдж в UI): не наше последнее сообщение
        # и не отказ. У habr ``applicant_state`` всегда None — отказ
        # детектим по тексту последнего сообщения.
        if not row.last_message_outgoing and not is_refusal_text(
            row.last_message_text or row.last_message_preview
        ):
            relevant += 1
        try:
            chat, was_created, last_msg_changed = await _upsert_chat_and_last_message(
                db, user_id=user_id, row=row,
            )
        except Exception:
            logger.exception(
                "habr upsert chat failed: user=%s login=%s",
                user_id, row.external_id,
            )
            continue
        saved += 1
        if was_created:
            created += 1
        if was_created or last_msg_changed:
            any_changed = True
        if last_msg_changed:
            to_prefetch.append(chat.external_id)

    if any_changed:
        await notify_chat_change(db, user_id)

    return {
        "seen": seen,
        "relevant": relevant,
        "saved": saved,
        "created": created,
        "to_prefetch": to_prefetch,
    }


#: Сколько чатов с обновившимся «последним сообщением» пре-фетчим
#: за один тик синхронизации. См. аналогичный лимит в hh-синке.
_PREFETCH_NEW_CHATS_PER_CYCLE = 20

#: Максимальное число страниц со списком диалогов, которые мы листаем
#: за один тик. По 20 диалогов/страница это покрывает кейс активного
#: соискателя на 200 диалогов в неделю — здоровый верхний предел.
_MAX_CONVERSATION_PAGES = 10


async def sync_habr_chats_for_user(
    *,
    user_id: uuid.UUID,
    habr_email: str = "",
) -> dict[str, Any]:
    """End-to-end: поднять :class:`HabrClient` → выкачать ``/conversations``
    (с пагинацией) → записать в БД.

    Возвращает короткий dict со счётчиками (``seen`` / ``relevant`` /
    ``saved`` / ``created``) — фронту он не критичен, но удобен в
    логах и в ``/api/chats/sync`` для отладки.

    Сетевые ошибки протекают наверх — caller (``chats_sync``-роут)
    их различает: :class:`HabrAuthError` → 401, остальное → 500.
    """
    from app.services.job_sites.habr import HabrAuthError, HabrClient

    prefetch_payloads: list[tuple[str, dict]] = []
    alias: str | None = None
    aggregated_stats = {
        "seen": 0,
        "relevant": 0,
        "saved": 0,
        "created": 0,
    }
    async with HabrClient(email=habr_email, user_id=user_id) as client:
        # Проверяем сессию через лёгкий /users/me — параллельно
        # забираем alias для маппинга AUTHOR_ME/AUTHOR_THEM.
        try:
            me = await client.fetch_users_me()
        except HabrAuthError:
            raise
        user_obj = me.get("user") if isinstance(me, dict) else None
        if isinstance(user_obj, dict):
            a = user_obj.get("alias")
            if isinstance(a, str) and a:
                alias = a

        # Тянем страницы /conversations по одной, прерываемся
        # при пустой странице или достижении totalPages.
        seen_logins: set[str] = set()
        page = 1
        while page <= _MAX_CONVERSATION_PAGES:
            html = await client.fetch_conversations_html(page=page)
            conversations, meta = extract_conversations_from_html(html)
            if not conversations:
                break

            # Дедуп: если habr на разных страницах возвращает один и
            # тот же login (бывает при изменении сортировки между
            # запросами), считаем его только один раз.
            new_items = []
            for c in conversations:
                login = (c.get("login") or "").strip() if isinstance(c, dict) else ""
                if not login or login in seen_logins:
                    continue
                seen_logins.add(login)
                new_items.append(c)
            if not new_items:
                break

            async with session_scope() as db:
                stats = await sync_habr_chats_from_payload(
                    db,
                    user_id=user_id,
                    items=new_items,
                    current_user_alias=alias,
                )
            aggregated_stats["seen"] += stats["seen"]
            aggregated_stats["relevant"] += stats["relevant"]
            aggregated_stats["saved"] += stats["saved"]
            aggregated_stats["created"] += stats["created"]

            # Пре-фетч полной истории для свежих диалогов (внутри
            # того же HabrClient-сеанса, один cookie-jar).
            for ext_id in stats["to_prefetch"][:_PREFETCH_NEW_CHATS_PER_CYCLE]:
                if not ext_id:
                    continue
                try:
                    payload = await client.fetch_chat_messages_page(
                        login=ext_id, page=1,
                    )
                except HabrAuthError:
                    logger.warning(
                        "habr prefetch /chat/messages: сессия протухла "
                        "(user=%s login=%s)", user_id, ext_id,
                    )
                    break
                except Exception:
                    logger.exception(
                        "habr prefetch /chat/messages failed: user=%s login=%s",
                        user_id, ext_id,
                    )
                    continue
                prefetch_payloads.append((ext_id, payload))

            # Пагинация: если total/current явно есть — слушаемся;
            # иначе двигаемся, пока страница что-то возвращает.
            cur = meta.get("currentPage") if isinstance(meta, dict) else None
            tot = meta.get("totalPages") if isinstance(meta, dict) else None
            if isinstance(cur, int) and isinstance(tot, int):
                if cur >= tot:
                    break
                page = cur + 1
            else:
                page += 1

    if prefetch_payloads:
        async with session_scope() as db:
            for ext_id, payload in prefetch_payloads:
                chat = (
                    await db.execute(
                        select(Chat).where(
                            Chat.user_id == user_id,
                            Chat.service == SERVICE_HABR,
                            Chat.external_id == ext_id,
                        )
                    )
                ).scalar_one_or_none()
                if chat is None:
                    continue
                try:
                    inserted = await sync_habr_chat_messages_from_payload(
                        db,
                        chat=chat,
                        payload=payload,
                        current_user_alias=alias,
                    )
                    if inserted:
                        logger.info(
                            "habr prefetch: user=%s chat=%s inserted=%s",
                            user_id, chat.id, inserted,
                        )
                except Exception:
                    logger.exception(
                        "habr prefetch write failed: user=%s login=%s",
                        user_id, ext_id,
                    )

    logger.info(
        "habr chats sync: user=%s seen=%s relevant=%s saved=%s "
        "created=%s prefetched=%s",
        user_id,
        aggregated_stats["seen"],
        aggregated_stats["relevant"],
        aggregated_stats["saved"],
        aggregated_stats["created"],
        len(prefetch_payloads),
    )
    return aggregated_stats


# Реэкспорт для удобства caller'ов (chat_status badge для habr рисуется
# тем же утильником, что и для hh: разница только в источнике applicant_state).
__all__ = [
    "HabrAuthError",  # type: ignore[name-defined]
    "REJECTED_APPLICANT_STATES",
    "_Devalue",
    "extract_conversations_from_html",
    "sync_habr_chats_for_user",
    "sync_habr_chats_from_payload",
    "sync_habr_chat_messages_for_user",
    "sync_habr_chat_messages_from_payload",
    "mark_habr_chat_read_for_user",
    "send_habr_chat_message_for_user",
    "is_refusal_text",
]
