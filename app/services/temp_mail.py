"""Клиент к https://temp.coda.ink/ — сервису временных почтовых ящиков.

REST-документация: https://temp.coda.ink/api-docs.

Что нам нужно от сервиса в проекте:

* Иметь под рукой «технический» почтовый ящик на каждого пользователя
  (см. ``users.temp_email``). На него мы будем регистрировать аккаунты
  на job-площадках (habr и т.д.), а потом менять email на основной.
* Иметь возможность ПОСТОЯННО читать inbox этого ящика — habr/площадки
  присылают коды подтверждения, и юзер сам ничего вводить не должен.

Особенности API, важные для интеграции:

* ``POST /v1/address`` отдаёт ``{address, token}``. ``address`` живёт
  бесконечно (для ``provider=tempmail``), ``token`` (``tm_at_…``) —
  ЕДИНСТВЕННЫЙ ключ доступа к inbox этого адреса. Сервис специально
  пишет в доке: «Save the token. It's the only way to read emails …
  it's not retrievable later». Эмпирически даже владелец общего
  API-ключа (``tm_…``) НЕ может читать чужой inbox без per-address
  токена — ответ 403 ``Address token required``. Поэтому мы храним
  токен рядом с email-ом в БД (``users.temp_email_token``).
* ``GET /v1/address/{addr}/emails`` — список писем (метаданные).
* ``GET /v1/email/{id}``                 — тело конкретного письма.
* Лимиты: 20 ящиков на ключ, 120 read-запросов/мин с API-ключом
  (см. ``app.config.TEMP_MAIL_API_TOKEN``). Без ключа — 3 и 20.

Сигнатуры публичных функций намеренно держат тонкую границу: каллеры
(``app.db.users``, ``app.services.job_sites.habr.auth_flow``) ничего
не знают о Bearer-токенах и URL'ах — только email + token (+ опционально
фильтры). Это позволяет в любой момент переехать на другой сервис без
правок в каллерах.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import aiohttp

from app.config import TEMP_MAIL_API_TOKEN, TEMP_MAIL_BASE_URL

logger = logging.getLogger(__name__)


# Тайм-аут на каждый HTTP-запрос. Сервис обещает ``~5s`` доставку и
# держит push-bridge через Gmail API, так что секунд 15 запас «с горкой».
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)

# Сколько символов кода считаем валидным. У habr — 6-значный код
# (подтверждено реальным habr-cabinet HTML: ``maxlength="6"`` на
# инпуте подтверждения смены email). У других площадок встречаются
# 4-значные. Берём вилку, чтобы regex ловил оба.
_CODE_MIN_DIGITS = 4
_CODE_MAX_DIGITS = 8
_CODE_RE = re.compile(rf"\b(\d{{{_CODE_MIN_DIGITS},{_CODE_MAX_DIGITS}}})\b")


@dataclass(frozen=True, slots=True)
class TempMailbox:
    """Свежесозданный ящик: адрес + персональный токен на чтение.

    Возвращается из :func:`create_temp_email`. ``token`` нужно сохранить
    в БД (``users.temp_email_token``) — без него inbox этого ящика
    больше никем не прочитается.
    """

    email: str
    token: str


# ── низкоуровневые HTTP-обёртки ──────────────────────────────────────


def _api_key_headers() -> dict[str, str]:
    """Заголовки для запросов, идущих от имени глобального API-ключа.

    Используется только при создании адреса (``POST /v1/address``):
    адреса, созданные с этим ключом, не имеют автоэкспайра и
    подпадают под повышенные rate limits.

    Если ключ не задан (``TEMP_MAIL_API_TOKEN==""``) — отдаём без
    Authorization; сервис разрешает анонимное создание адресов,
    просто с жёсткими лимитами (3/min, 3 active per IP).
    """
    headers = {"Accept": "application/json"}
    if TEMP_MAIL_API_TOKEN:
        headers["Authorization"] = f"Bearer {TEMP_MAIL_API_TOKEN}"
    return headers


def _address_headers(token: str) -> dict[str, str]:
    """Заголовки для запросов, привязанных к конкретному адресу.

    Сервис требует Bearer-токен этого адреса (``tm_at_…``) — общий
    API-ключ для чтения inbox-а НЕ принимается.
    """
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }


# ── публичные хелперы ───────────────────────────────────────────────


async def create_temp_email(
    *,
    provider: str = "tempmail",
) -> TempMailbox:
    """Создать новый временный почтовый ящик.

    Возвращает :class:`TempMailbox` — кортеж ``(email, token)``. Токен
    нужно сохранить в БД (``users.temp_email_token``), иначе доступ к
    inbox этого ящика будет потерян окончательно.

    ``provider`` оставлен опциональным аргументом ради явной
    управляемости: по умолчанию ``tempmail`` (бесконечный TTL); при
    необходимости можно перейти на ``gmail`` (15-минутный TTL,
    подходит для одноразовых регистраций), но для нашего сценария
    (habr держит email юзера месяцами) это плохой выбор.
    """
    url = f"{TEMP_MAIL_BASE_URL}/address"
    payload = {"provider": provider}

    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as s:
        async with s.post(url, headers=_api_key_headers(), json=payload) as r:
            data = await r.json(content_type=None)
            if r.status != 200 or not data.get("success"):
                raise RuntimeError(
                    f"temp.coda.ink: POST /v1/address failed "
                    f"({r.status}): {data.get('error') or data!r}"
                )

    body = data.get("data") or {}
    email = body.get("address") or ""
    token = body.get("token") or ""
    if not email or not token:
        raise RuntimeError(
            f"temp.coda.ink: response без address/token: {data!r}"
        )

    logger.info(
        "[temp_mail] created %s (provider=%s, token=tm_at_…%s)",
        email, body.get("provider", provider), token[-6:],
    )
    return TempMailbox(email=email, token=token)


async def fetch_inbox(
    email: str,
    token: str,
    *,
    with_body: bool = False,
) -> list[dict[str, Any]]:
    """Список писем в ящике (новые сверху).

    ``with_body=False`` (по умолчанию) — отдаёт только метаданные
    (``id``, ``from_address``, ``subject``, ``created_at``); этого
    хватает для поиска нужного письма и одного дополнительного
    запроса за телом. ``with_body=True`` — дополнительно подтянет
    ``body_text`` каждого письма (за счёт N+1 GET'ов; используем
    только в :func:`extract_code_from_inbox`, где это нужно).

    Бросает ``RuntimeError`` на любую не-2xx (включая 403, если
    токен не принадлежит ящику, и 404, если ящик удалили). Каллеры
    в habr-флоу должны просто перепробовать через пару секунд.
    """
    if not email:
        raise ValueError("fetch_inbox: пустой email")
    if not token:
        raise ValueError("fetch_inbox: пустой token (не передан из БД?)")

    url = f"{TEMP_MAIL_BASE_URL}/address/{email}/emails"
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as s:
        async with s.get(url, headers=_address_headers(token)) as r:
            data = await r.json(content_type=None)
            if r.status != 200 or not data.get("success"):
                raise RuntimeError(
                    f"temp.coda.ink: GET /v1/address/{email}/emails "
                    f"failed ({r.status}): {data.get('error') or data!r}"
                )
        messages: list[dict[str, Any]] = list(data.get("data") or [])

        if with_body and messages:
            for msg in messages:
                msg_id = msg.get("id")
                if not msg_id:
                    continue
                async with s.get(
                    f"{TEMP_MAIL_BASE_URL}/email/{msg_id}",
                    headers=_address_headers(token),
                ) as br:
                    body = await br.json(content_type=None)
                if br.status == 200 and body.get("success"):
                    msg.update(body.get("data") or {})

    logger.debug(
        "[temp_mail] fetch_inbox(%s) -> %d msgs (with_body=%s)",
        email, len(messages), with_body,
    )
    return messages


async def fetch_email(email_id: str, token: str) -> dict[str, Any]:
    """Тело конкретного письма по его id.

    Возвращает словарь с ключами ``id``, ``from``, ``to``,
    ``subject``, ``body_text``, ``body_html``, ``created_at``.
    """
    if not email_id:
        raise ValueError("fetch_email: пустой email_id")
    if not token:
        raise ValueError("fetch_email: пустой token")

    url = f"{TEMP_MAIL_BASE_URL}/email/{email_id}"
    async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as s:
        async with s.get(url, headers=_address_headers(token)) as r:
            data = await r.json(content_type=None)
            if r.status != 200 or not data.get("success"):
                raise RuntimeError(
                    f"temp.coda.ink: GET /v1/email/{email_id} "
                    f"failed ({r.status}): {data.get('error') or data!r}"
                )
    return data.get("data") or {}


async def extract_code_from_inbox(
    email: str,
    token: str,
    *,
    sender_contains: str | None = None,
    subject_contains: str | None = None,
) -> str | None:
    """Найти в inbox код подтверждения и вернуть его строкой.

    Алгоритм:

    1. Тянем список писем (новые сверху, ``with_body=True``).
    2. Отсеиваем письма, у которых отправитель/тема не совпадают с
       фильтрами (если они заданы — например, ``sender_contains="habr"``
       чтобы не ловить промо-рассылки от temp.coda.ink самого себя).
    3. В первом подходящем ищем 4–8-значное число (``\\b\\d{4,8}\\b``)
       в ``body_text``, либо как фоллбек — в ``subject``.

    Возвращает строку с кодом или ``None``, если ничего не нашлось.
    Для интерактивного ожидания письма используй
    :func:`wait_for_code` — он повторно опрашивает inbox.
    """
    messages = await fetch_inbox(email, token, with_body=True)
    if not messages:
        return None

    needle_sender = (sender_contains or "").lower()
    needle_subject = (subject_contains or "").lower()

    for msg in messages:
        sender = (msg.get("from") or msg.get("from_address") or "").lower()
        subject = (msg.get("subject") or "").lower()
        if needle_sender and needle_sender not in sender:
            continue
        if needle_subject and needle_subject not in subject:
            continue
        for text in (msg.get("body_text") or "", msg.get("subject") or ""):
            match = _CODE_RE.search(text)
            if match:
                return match.group(1)
    return None


async def wait_for_code(
    email: str,
    token: str,
    *,
    sender_contains: str | None = None,
    subject_contains: str | None = None,
    timeout: float = 120.0,
    poll_interval: float = 3.0,
) -> str | None:
    """Опрашивать inbox пока не появится письмо с кодом (или вышло время).

    Используется habr-флоу: после ``start_register`` мы ждём письмо от
    ``noreply@habr.com`` с 4-значным кодом регистрации (см. описание
    в ``app/services/job_sites/habr/auth_flow.py``).

    Возвращает строку с кодом или ``None`` при тайм-ауте. Никаких
    исключений на сетевых ошибках не пробрасывает: они логируются,
    итерация продолжается — это безопаснее, чем падать на временном
    503 от провайдера.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            code = await extract_code_from_inbox(
                email,
                token,
                sender_contains=sender_contains,
                subject_contains=subject_contains,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[temp_mail] poll inbox failed: %s", exc)
            code = None
        if code:
            return code
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(poll_interval)


async def delete_mailbox(email: str, token: str) -> bool:
    """Удалить ящик и все его письма (``DELETE /v1/address/{addr}``).

    Используется опционально — например, если юзер сменил основной
    email и временный больше не нужен. Возвращает ``True`` при успехе,
    ``False`` если сервис ответил ошибкой.
    """
    url = f"{TEMP_MAIL_BASE_URL}/address/{email}"
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as s:
            async with s.delete(url, headers=_address_headers(token)) as r:
                ok = r.status == 200
                if not ok:
                    body = await r.text()
                    logger.warning(
                        "[temp_mail] DELETE /v1/address/%s -> %s %s",
                        email, r.status, body[:200],
                    )
                return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("[temp_mail] delete_mailbox(%s) failed: %s", email, exc)
        return False
