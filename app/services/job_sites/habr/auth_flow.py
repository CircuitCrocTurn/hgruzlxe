"""Habr connect flow — orchestrator подключения площадки habr.

Принцип работы (со стороны бэка, шаги по порядку):

1. Юзер кликает «Подключить» на карточке habr в ``/settings``.
2. Бэк смотрит, есть ли у юзера ОСНОВНОЙ email (из распарсенного резюме,
   ``Resume.parsed_data["email"]``).

   * Если основного email нет → отвечаем 400 ``main_email_required``,
     UI показывает «укажите email в резюме».

3. Бэк делает «зондирующий» логин на habr под основным email юзера и
   рандомным паролем — задача чисто диагностическая: узнать, ЕСТЬ ли
   уже такой аккаунт на habr.

   * habr ответил «такой аккаунт уже существует / неверный пароль» →
     :func:`check_habr_email_exists` возвращает ``True``.
   * habr ответил «нет такого аккаунта» → возвращает ``False``.

4. Развилка:

   А) **Аккаунта на habr НЕТ.** Регистрируемся на habr на нашу
      :func:`temp_email` пользователя с :func:`platform_password`.
      После успешной регистрации идём в ``/ru/cabinet`` и запускаем
      смену email на ОСНОВНОЙ email юзера. habr пришлёт SMS-код на
      основную почту (или на телефон, привязанный к новому аккаунту —
      зависит от площадки). Юзер вводит код в UI; код приходит в
      ``/api/settings/platforms/verify``, мы дёргаем
      :func:`confirm_email_change` и помечаем учётку ``active``.

   Б) **Аккаунт на habr ЕСТЬ.** Запускаем восстановление пароля по
      основному email юзера (POST ``/ru/restore-password``). habr
      шлёт письмо со ссылкой вида
      ``https://account.habr.com/ru/restore-password/<token>``. Юзер
      вводит код из письма в UI, мы дёргаем
      :func:`confirm_password_recovery` со СВЕЖИМ
      :func:`platform_password` юзера — habr меняет пароль, аккаунт
      «легально» переходит к нам.

ВНИМАНИЕ. Все четыре низкоуровневые функции ниже —
``check_habr_email_exists``, ``register_habr_account``,
``initiate_email_change``, ``initiate_password_recovery``,
``confirm_email_change``, ``confirm_password_recovery`` — сейчас
ЗАГЛУШКИ. Они НЕ ходят в habr. Они логируют намерение, возвращают
правдоподобные структуры и хранят демо-код в памяти процесса. Когда
у нас будет точная структура реальных HTTP-запросов к habr —
вписываем тело каждой из этих функций; вышестоящий orchestrator
(``start_habr_connect_flow`` / ``verify_habr_connect_flow``) и
API-маршруты остаются прежними.
"""
from __future__ import annotations

import logging
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from app.services.job_sites.habr import (
    ACCOUNT_BASE,
    USER_AGENT,
    HabrClient,
    YandexCaptchaSolver,
    derive_career_nickname,
    generate_password,
)
from app.services.temp_mail import wait_for_code

logger = logging.getLogger("offerday.habr.auth_flow")


# Тайм-аут для одиночных запросов в account.habr.com (без капчи).
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Сколько ждём письмо с кодом регистрации/смены email в temp.coda.ink.
# Habr доставляет письмо ~10–30 секунд; запас x4 на flaky прод.
_INBOX_WAIT_SECONDS = 120.0

# Заголовки для AJAX-запросов на account.habr.com — habr различает
# AJAX/обычный submit и без них может вернуть HTML вместо JSON.
_HABR_AJAX_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": ACCOUNT_BASE,
}


def _common_headers(referer: str | None = None) -> dict[str, str]:
    h = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    }
    if referer:
        h["Referer"] = referer
    return h


async def _discover_ident_token(
    session: aiohttp.ClientSession,
) -> str:
    """Получить page_token из 302 на ``account.habr.com/``.

    habr редиректит анонима с ``/`` на ``/ru/ident/{token1}`` — этот
    ``token1`` далее используется как path-параметр для login
    (``/ru/ident/in/{token1}``), register (``/ru/register/start/{token1}``)
    и restore-password (``/ru/restore-password/start/{token1}``).
    """
    async with session.get(
        f"{ACCOUNT_BASE}/",
        allow_redirects=False,
    ) as r:
        location = r.headers.get("Location") or ""
    token = urlparse(location).path.rsplit("/", 1)[-1]
    if not token:
        raise RuntimeError(
            f"habr: не удалось извлечь ident-token из Location={location!r}"
        )
    return token


async def _extract_sitekey(
    session: aiohttp.ClientSession,
    page_url: str,
) -> str:
    """Скачать страницу формы и достать ``data-sitekey`` Yandex-капчи.

    Применяется к страницам ``/ru/ident/{t}``, ``/ru/register/{t}``,
    ``/ru/restore-password/{t}`` — все три кладут sitekey в один и
    тот же ``<div data-captcha="yandex" data-sitekey="…">``.
    """
    async with session.get(page_url) as r:
        r.raise_for_status()
        html = await r.text()
    # Сперва пробуем BS4-парсер: устойчив к мелким изменениям шаблона.
    soup = BeautifulSoup(html, "html.parser")
    captcha_div = soup.find("div", attrs={"data-captcha": "yandex"})
    if captcha_div and captcha_div.get("data-sitekey"):
        return str(captcha_div["data-sitekey"])
    # Фоллбек: regex (как в asd.py — на случай если шаблон другой).
    match = re.search(r'data-sitekey="([^"]+)"', html)
    if match:
        return match.group(1)
    raise RuntimeError(f"habr: на странице {page_url} не найден data-sitekey")


def _is_already_registered(resp: dict[str, Any]) -> bool:
    """Habr вернул на ``/ru/register/start`` «email уже занят»?

    Сравнивать формально с ``errors.email == "Аккаунт с такой почтой
    уже зарегистрирован"`` не хочется — habr изредка меняет текст и
    может локализовать его на en. Поэтому ищем сигнал по
    ключевым подстрокам в repr(resp).lower(): «уже зарегистр» /
    «already.*registered» / «email.*taken». Любая из них — повод
    пойти по login-fallback вместо ``raise``.
    """
    if resp.get("success"):
        return False
    blob = repr(resp).lower()
    return (
        "уже зарегистр" in blob
        or "already registered" in blob
        or "already_registered" in blob
        or "email_taken" in blob
        or "email.taken" in blob
    )


def _is_nickname_unavailable(resp: dict[str, Any]) -> bool:
    """Habr на ``/ru/register/start`` сказал «никнейм недоступен»?

    Появляется, когда habr-аккаунт с таким `login`-ом уже
    существует (например — мы сами создавали раньше, или это
    реальный юзер с habr.com). Сигнал — `errors.nickname` с
    подстрокой «недоступен» / «taken» / «invalid». На такой ответ
    ретраим start_register с рандомизированным никнеймом.
    """
    if resp.get("success"):
        return False
    errors = resp.get("errors")
    if not isinstance(errors, dict) or "nickname" not in errors:
        return False
    nickname_err = str(errors.get("nickname") or "").lower()
    return (
        "недоступен" in nickname_err
        or "занят" in nickname_err
        or "taken" in nickname_err
        or "invalid" in nickname_err
        or "unavailable" in nickname_err
    )


def _randomized_nickname(base: str) -> str:
    """Дозалить base случайным суффиксом так, чтобы влезть в лимиты habr.

    Лимит habr — 30 символов, разрешены [a-z0-9_]. Берём base,
    обрезаем до 22 символов на всякий случай, дописываем `_` и
    7 нижне-регистровых alnum-символов (~36^7 = 78b вариантов).
    """
    import secrets, string
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(secrets.choice(alphabet) for _ in range(7))
    safe_base = "".join(c for c in base.lower() if c.isalnum() or c == "_")
    if not safe_base:
        safe_base = "user"
    return f"{safe_base[:22]}_{suffix}"


async def _get_temp_email_token(email: str) -> str | None:
    """Достать per-address Bearer-токен temp.coda.ink из БД по email.

    Возвращает строку токена или ``None``, если такого ящика у нас
    нет (например, был создан до миграции ``b5e7d1c92a08``). В этом
    случае писать в habr inbox мы всё ещё можем, но прочесть его
    обратно не получится — флоу регистрации упадёт на ожидании кода.
    """
    # Импорты делаем лениво, чтобы избежать циклической зависимости
    # ``app.db.models -> app.services.job_sites.habr -> auth_flow``.
    from sqlalchemy import select

    from app.db.models.user import User
    from app.db.session import session_scope

    async with session_scope() as session:
        result = await session.execute(
            select(User.temp_email_token).where(User.temp_email == email)
        )
        return result.scalar_one_or_none()


# ── DTOs / типы флоу ─────────────────────────────────────────────────────


FlowType = Literal["email_change", "password_recovery"]


@dataclass
class HabrConnectFlow:
    """Состояние «в процессе подключения habr» для одного юзера.

    Хранится в in-memory словаре :data:`_PENDING_FLOWS` пока юзер не
    введёт код подтверждения (или не отменит флоу). После рестарта
    процесса флоу теряется — пользователь должен будет нажать
    «Подключить» заново. Это сознательное упрощение, аналогично тому
    как сейчас работает ``_PENDING_OTPS`` для остальных площадок.
    """

    user_id: uuid.UUID
    flow_type: FlowType
    # Основной email юзера (куда habr шлёт код подтверждения).
    main_email: str
    # Демо-код, который мы «как бы» получили из habr. На реальном API
    # этого поля не будет — код прийдёт юзеру на почту, а наш
    # бэкенд получит ``True/False`` в ответ на верификацию.
    expected_code: str
    started_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    # Сюда положим всё, что вернёт реальный habr (например, token
    # из ссылки восстановления). Сейчас пусто.
    extra: dict[str, Any] = field(default_factory=dict)


# Per-process state: user_id → HabrConnectFlow. Только одно активное
# флоу на юзера. При повторном «Подключить» предыдущее перезаписываем.
_PENDING_FLOWS: dict[uuid.UUID, HabrConnectFlow] = {}


# ── Низкоуровневые ЗАГЛУШКИ HTTP-вызовов к habr ──────────────────────────


def _new_demo_code() -> str:
    """6-значный код для демо-режима (хабр присылает 6 цифр)."""
    return f"{secrets.randbelow(1_000_000):06d}"


async def check_habr_email_exists(email: str, password: str) -> bool:
    """Есть ли уже аккаунт на habr под этим ``email``?

    Делаем «зондирующий» логин: берём ident-token с
    ``account.habr.com/``, тащим sitekey капчи с ident-страницы,
    решаем её через 2Captcha и POST'им логин с заведомо неверным
    случайным паролем. По ответу:

    * ``success=true`` (на пустой пароль такое не бывает, но
      покрываем сценарий «случайно угадали») → аккаунт ЕСТЬ.
    * ``success=false`` + ошибка на ``password`` («неверный пароль»,
      ``password.invalid``, ``invalid_credentials``) → аккаунт ЕСТЬ
      (habr знает email и предъявляет ошибку только по паролю).
    * ``success=false`` + ошибка на ``email`` (``not_found``,
      «не зарегистрирован», ``email.not_found``) → аккаунта НЕТ.
    * Любая другая «не-success» (капча, rate-limit, ошибка сервера) —
      пессимистично считаем, что аккаунта НЕТ: тогда флоу пойдёт по
      ветке А (регистрация на temp-email), что безопаснее, чем
      сломать восстановление пароля для несуществующего юзера.
    """
    if not email:
        return False

    solver = YandexCaptchaSolver()
    async with aiohttp.ClientSession(
        headers=_common_headers(),
        timeout=_HTTP_TIMEOUT,
    ) as s:
        token1 = await _discover_ident_token(s)
        ident_form_url = f"{ACCOUNT_BASE}/ru/ident/{token1}"
        sitekey = await _extract_sitekey(s, ident_form_url)

        captcha = await solver.solve(ident_form_url, sitekey)
        if not captcha.success or not captcha.token:
            logger.warning(
                "[habr.auth_flow] check_habr_email_exists(%s): "
                "captcha failed (%s)", email, captcha.error,
            )
            return False

        # Случайный пароль — он намеренно не совпадёт ни с одним
        # реальным; нас интересует только КЛАСС ошибки от habr.
        async with s.post(
            f"{ACCOUNT_BASE}/ru/ident/in/{token1}",
            data={
                "email": email,
                "password": password,
                "smart-token": captcha.token,
            },
            headers={**_HABR_AJAX_HEADERS, "Referer": ident_form_url},
        ) as r:
            try:
                data = await r.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                text = (await r.text())[:300]
                logger.warning(
                    "[habr.auth_flow] ident/in вернул не-JSON (%s): %s",
                    r.status, text,
                )
                return False

    if data.get("success") is True:
        # Маловероятно (мы передали случайный пароль), но логично:
        # если вдруг angeschal — аккаунт точно существует.
        return True

    errors = data.get("errors") or {}
    raw_blob = repr(data).lower()

    # Сильные сигналы «такой email habr не знает».
    email_missing = (
        "email" in errors and any(
            "not_found" in str(v).lower() or "не зарегистр" in str(v).lower()
            for v in (errors["email"] if isinstance(errors["email"], list)
                     else [errors["email"]])
        )
    ) or "не зарегистр" in raw_blob or "not_found" in raw_blob
    if email_missing:
        return False

    # Сильные сигналы «email есть, неверный пароль».
    password_error = (
        "password" in errors
        or "неверный пароль" in raw_blob
        or "invalid_credentials" in raw_blob
        or "password.invalid" in raw_blob
    )
    if password_error:
        return True

    # Неоднозначный ответ — лучше считать, что аккаунта НЕТ и пойти
    # по более «прощающей» ветке регистрации.
    logger.info(
        "[habr.auth_flow] check_habr_email_exists(%s): ambiguous resp=%r",
        email, data,
    )
    return False


async def register_habr_account(
    *,
    temp_email: str,
    password: str,
) -> dict[str, Any]:
    """Зарегистрировать аккаунт на habr под temp-почту.

    Шаги (повторяет руками то, что делает :class:`HabrClient`):

    1. ``discover_register_flow`` → ``register_link / page_token / sitekey``.
    2. ``start_register`` — habr отправляет 6-значный код на
       ``temp_email``.
    3. Поллим inbox temp.coda.ink на письма от ``noreply@habr.com``,
       вынимаем код.
    4. ``finish_register(page_token, code)`` — habr подтверждает
       регистрацию и ставит ``account.habr.com``-cookies.

    Cookies после регистрации сохраняются HabrClient'ом в in-memory
    ``_OTP_BRIDGE`` (т.к. ``user_id=None``) и оттуда подхватываются
    следующим вызовом :func:`initiate_email_change` под тем же
    ``temp_email``.

    Возвращает словарь с ``ok``, ``registered_email`` и сырыми
    ответами habr для ``start_register`` / ``finish_register``
    (полезно для отладки).
    """
    token_at = await _get_temp_email_token(temp_email)
    if not token_at:
        raise RuntimeError(
            f"register_habr_account: нет temp_email_token для {temp_email} "
            "в БД — пересоздайте ящик через ensure_platform_credentials"
        )

    # До 5 попыток: первый раз с дефолтным никнеймом (email.split("@")[0]),
    # дальше — с рандомизированным, если habr пожаловался на коллизию.
    # 5 попыток = 5 капч (каждая ~0.005-0.01$), но реально на 99%
    # случаев должно хватить второй (рандомные суффиксы из 36^7 ~ 78
    # миллиардов вариантов). Капчи на ретраях не share-ятся — habr
    # инвалидирует smart-token после первого использования.
    _MAX_REGISTER_RETRIES = 5

    async with HabrClient(email=temp_email) as c:
        register_link, page_token, sitekey = await c.discover_register_flow()

        start_resp: dict[str, Any] = {}
        attempted_nicknames: list[str] = []
        current_nickname: str | None = None  # None → дефолт в start_register
        for attempt in range(_MAX_REGISTER_RETRIES):
            start_resp = await c.start_register(
                register_link=register_link,
                page_token=page_token,
                sitekey=sitekey,
                email=temp_email,
                password=password,
                nickname=current_nickname,
            )
            attempted_nicknames.append(
                current_nickname or temp_email.split("@")[0]
            )
            if start_resp.get("success"):
                break
            if _is_nickname_unavailable(start_resp):
                # Ретраим с рандомизированным никнеймом. Дополнительно
                # после успешной регистрации мы всё равно перепишем
                # никнейм через change_career_nickname() в
                # verify_habr_connect_flow — финальное значение
                # детерминировано (`derive_career_nickname(user_id)`),
                # так что временный suffix-никнейм юзеру не видно.
                base = temp_email.split("@")[0]
                current_nickname = _randomized_nickname(base)
                logger.info(
                    "[habr.auth_flow] register_habr_account(%s): "
                    "никнейм недоступен (attempt=%d/%d), ретрай с "
                    "nickname=%s",
                    temp_email, attempt + 1, _MAX_REGISTER_RETRIES,
                    current_nickname,
                )
                continue
            # Любая другая ошибка — не повторяем (включая
            # already-registered: на этой ветке temp_email фиксирован
            # и каптчи тратить смысла нет).
            break

        if not start_resp.get("success"):
            # habr возвращает ошибку «Аккаунт с такой почтой уже
            # зарегистрирован» если temp_email уже использовался для
            # регистрации (юзер уже жал «Подключить» и аккаунт создан).
            # Это НЕ ошибка флоу: с нашим детерминированным
            # ``platform_password`` (=``generate_platform_password(user.id)``)
            # мы всё ещё знаем пароль и можем просто залогиниться.
            if _is_already_registered(start_resp):
                logger.info(
                    "[habr.auth_flow] register_habr_account(%s): "
                    "аккаунт уже существует, делаю login fallback",
                    temp_email,
                )
                login_ok = await c.login(
                    page_token=page_token,
                    sitekey=sitekey,
                    email=temp_email,
                    password=password,
                )
                if not login_ok:
                    raise RuntimeError(
                        f"habr.register/start: аккаунт {temp_email} уже "
                        f"зарегистрирован, но логин с известным паролем "
                        f"провалился — нужно сбросить temp_email"
                    )
                return {
                    "ok": True,
                    "registered_email": temp_email,
                    "start_response": start_resp,
                    "finish_response": None,
                    "logged_in_existing": True,
                }
            if _is_nickname_unavailable(start_resp):
                raise RuntimeError(
                    f"habr.register/start: за {_MAX_REGISTER_RETRIES} "
                    f"попыток не подобрали свободный никнейм для "
                    f"{temp_email}; пробовали {attempted_nicknames!r}; "
                    f"последний ответ: {start_resp!r}"
                )
            raise RuntimeError(
                f"habr.register/start не успешен: {start_resp!r}"
            )

        code = await wait_for_code(
            temp_email,
            token_at,
            sender_contains="habr",
            timeout=_INBOX_WAIT_SECONDS,
        )
        if not code:
            raise RuntimeError(
                f"habr.register: код подтверждения не пришёл в "
                f"{_INBOX_WAIT_SECONDS:.0f}s на {temp_email}"
            )

        finish_resp = await c.finish_register(page_token, code)
        ok = bool(finish_resp.get("success"))

    logger.info(
        "[habr.auth_flow] register_habr_account(%s) -> ok=%s",
        temp_email, ok,
    )
    return {
        "ok": ok,
        "registered_email": temp_email,
        "start_response": start_resp,
        "finish_response": finish_resp,
        "logged_in_existing": False,
    }


# Реальные эндпоинты habr-cabinet для смены email подтверждены
# по HAR-логу (см. attachments/account.habr.com_Archive_*.har):
#
#   POST /ru/cabinet/change-email/start
#       body: email=...&password=...&smart-token=...
#       headers: x-form-token, X-Requested-With: XMLHttpRequest
#       referer: /ru/cabinet
#       resp: {"success":true,"resend_in":120}
#
#   POST /ru/cabinet/change-email/finish
#       body: code=...
#       headers: x-form-token (свежий, регенерится per-request)
#       resp: {"success":true} либо
#             {"success":false,"errors":{"code":"Неверный или просроченный код"}}
#
# Раньше путь искался динамически (BS4-discovery формы), но habr-cabinet
# рендерит форму смены email через JS внутри модалки, и тегов
# ``<form action=...>`` в исходном HTML нет — поиск падал. Теперь
# хардкодим путь, а из cabinet-HTML тащим только динамические штуки
# (``x-form-token`` и sitekey Yandex-капчи).
_CABINET_URL = f"{ACCOUNT_BASE}/ru/cabinet"
_CHANGE_EMAIL_START_URL = f"{ACCOUNT_BASE}/ru/cabinet/change-email/start"
_CHANGE_EMAIL_FINISH_URL = f"{ACCOUNT_BASE}/ru/cabinet/change-email/finish"
_CHANGE_EMAIL_CANCEL_URL = f"{ACCOUNT_BASE}/ru/cabinet/change-email/cancel"


def _extract_form_token_for(html: str, action_url: str) -> str:
    """Достать ``data-token`` ровно у формы с указанным ``action``.

    Подтверждено реальным HTML habr-cabinet (см. логи пользователя):
    каждая ``<form>`` внутри ``/ru/cabinet`` несёт свой собственный
    ``data-token``, который habr-фронт затем шлёт в заголовке
    ``x-form-token``. Токены РАЗНЫЕ для разных действий:

      <form action=".../cabinet/change-email/start"  data-token="2mPM…">
      <form action=".../cabinet/change-email/finish" data-token="jD8G…">
      <form action=".../cabinet/change-email/cancel" data-token="EPOs…">
      <form action=".../cabinet/change-email/resend" data-token="GPvA…">
      <form action=".../cabinet/change-password"     data-token="JzL0…">

    Поэтому НЕЛЬЗЯ брать первый попавшийся ``data-token`` со страницы
    — нужно матчить именно по ``action``. Сравниваем по path, чтобы
    устойчиво работало и для абсолютного, и для относительного
    ``action``.
    """
    target_path = (
        urlparse(action_url).path if action_url.startswith("http") else action_url
    )

    soup = BeautifulSoup(html, "html.parser")
    for form in soup.find_all("form"):
        action = str(form.get("action") or "")
        if not action:
            continue
        action_path = urlparse(action).path if action.startswith("http") else action
        if action_path != target_path:
            continue
        token = form.get("data-token")
        if token:
            return str(token)
    return ""


def _extract_cabinet_sitekey(html: str) -> str:
    """Sitekey Yandex SmartCaptcha из cabinet-HTML.

    Виджет капчи habr рендерит на странице ``/ru/cabinet`` тем же
    атрибутом ``data-sitekey``, что и на ident/register. Если в HTML
    его нет (например, виджет монтируется только при сабмите формы),
    возвращаем пустую строку — caller заполнит fallback'ом известного
    sitekey'а habr.
    """
    soup = BeautifulSoup(html, "html.parser")
    captcha_div = soup.find("div", attrs={"data-captcha": "yandex"})
    if captcha_div and captcha_div.get("data-sitekey"):
        return str(captcha_div["data-sitekey"])
    m = re.search(r'data-sitekey="([^"]+)"', html)
    return m.group(1) if m else ""


# Фоллбек-sitekey: habr использует один и тот же ключ Yandex
# SmartCaptcha и для ident/register, и для cabinet/change-email
# (подтверждено HAR-логом). Используем, если из cabinet-HTML
# вытащить не удалось (виджет рендерится JS-ом по событию).
_HABR_FALLBACK_SITEKEY = (
    "ysc1_zgWuDVpgrG9kwB8QEfIkuWseZyEnRzHLCAPF2dwh1db6e985"
)


def _has_pending_email_change(html: str) -> bool:
    """Проверить, есть ли у аккаунта незавершённая смена email.

    habr-cabinet шаблонит блок ``chge-step1`` (ввод кода) ровно тогда,
    когда у юзера уже есть запрошенная смена email, ждущая ввода кода:

      <div id="chge-step0" hidden>...</div>
      <div id="chge-step1">...</div>    ← без hidden = pending
      <div id="chge-step2" hidden>...</div>

    Без сброса этого state'а повторный POST ``/change-email/start``
    ловит ``{'error':'Вы уже начали процесс смены адреса...'}``.
    """
    soup = BeautifulSoup(html, "html.parser")
    step1 = soup.find("div", id="chge-step1")
    return step1 is not None and step1.get("hidden") is None


async def _reset_pending_email_change_if_any(
    client: HabrClient,
    html: str,
) -> bool:
    """Если в ``html`` cabinet виден pending смены email — отменить.

    Возвращает ``True`` если был сделан запрос на ``/cancel`` (значит
    HTML cabinet после этого надо перефетчить — токены и состояние
    блоков обновятся). cancel — best-effort: на любую ошибку молча
    игнорируем, чтобы не блокировать happy-path.
    """
    if not _has_pending_email_change(html):
        return False

    cancel_token = _extract_form_token_for(html, _CHANGE_EMAIL_CANCEL_URL)
    if not cancel_token:
        return False

    try:
        async with client.session.post(
            _CHANGE_EMAIL_CANCEL_URL,
            data={},
            headers={
                **_HABR_AJAX_HEADERS,
                "Referer": _CABINET_URL,
                "x-form-token": cancel_token,
                "Content-Type":
                    "application/x-www-form-urlencoded; charset=UTF-8",
            },
        ) as r:
            await r.text()  # consume body
        logger.info(
            "[habr.auth_flow] _reset_pending_email_change_if_any: "
            "отменена незавершённая смена email"
        )
    except Exception as exc:  # noqa: BLE001 — best effort
        logger.warning(
            "[habr.auth_flow] _reset_pending_email_change_if_any: "
            "ошибка при отмене (игнор): %r", exc,
        )
    return True


async def _fetch_cabinet_meta(
    client: HabrClient,
    action_url: str,
) -> tuple[str, str]:
    """Скачать ``/ru/cabinet`` и вынуть ``(x-form-token, sitekey)``.

    ``action_url`` — URL формы, чей ``data-token`` нам нужен.
    Сейчас это либо ``_CHANGE_EMAIL_START_URL``, либо
    ``_CHANGE_EMAIL_FINISH_URL`` (см. ``_extract_form_token_for``).

    Поднимает ``RuntimeError`` если HTML вернулся, но нужной формы /
    токена не нашлось — это значит, что либо мы не залогинены (cookies
    битые), либо habr поменял разметку cabinet; в обоих случаях стоит
    руками посмотреть HTML-фрагмент в логе.
    """
    async with client.session.get(_CABINET_URL) as r:
        r.raise_for_status()
        html = await r.text()

    form_token = _extract_form_token_for(html, action_url)
    if not form_token:
        snippet = (html or "")[:800]
        raise RuntimeError(
            "habr.cabinet: не нашёл data-token формы с "
            f"action={action_url!r} в HTML /ru/cabinet. "
            f"Фрагмент: {snippet!r}"
        )

    sitekey = _extract_cabinet_sitekey(html) or _HABR_FALLBACK_SITEKEY
    return form_token, sitekey


async def initiate_email_change(
    *,
    temp_email: str,
    new_email: str,
    password: str,
) -> dict[str, Any]:
    """Запросить смену email-а habr-аккаунта на ``new_email``.

    Контракт: ВЫЗЫВАТЬ строго после :func:`register_habr_account`,
    т.к. cookies сессии лежат в ``_OTP_BRIDGE[temp_email]`` (см.
    HabrClient._restore_cookies_into).

    Шаги (подтверждены HAR-логом реального habr-cabinet):

    1. GET ``/ru/cabinet`` под кукой только что зарегистрированного
       аккаунта → вынимаем из HTML ``x-form-token`` и ``data-sitekey``.
    2. Решаем Yandex SmartCaptcha (тот же sitekey, что и на ident).
    3. POST ``/ru/cabinet/change-email/start`` с body
       ``email=NEW&password=PLATFORM_PASSWORD&smart-token=CAPTCHA``
       и заголовком ``x-form-token``. habr валидирует пароль (это
       та же ``platform_password``, которой регистрировались) и
       шлёт юзеру на ``new_email`` 6-значный код подтверждения.

    Возвращает ``{"ok", "raw", "endpoint", "demo_code": ""}``.
    ``demo_code`` обязателен, т.к. orchestrator читает его для
    ``flow.expected_code``; реальный код знает только habr (письмо
    юзеру), у нас его нет.
    """
    solver = YandexCaptchaSolver()
    async with HabrClient(email=temp_email) as c:
        # Сначала фетчим cabinet HTML — проверяем, нет ли pending смены
        # email с прошлого захода. Если есть — отменяем и перефетчиваем
        # HTML, потому что после ``/cancel`` habr перегенерит блок
        # ``chge-step0`` (и его ``data-token``).
        async with c.session.get(_CABINET_URL) as r:
            r.raise_for_status()
            html = await r.text()

        was_pending = await _reset_pending_email_change_if_any(c, html)
        if was_pending:
            async with c.session.get(_CABINET_URL) as r:
                r.raise_for_status()
                html = await r.text()

        form_token = _extract_form_token_for(html, _CHANGE_EMAIL_START_URL)
        if not form_token:
            snippet = (html or "")[:800]
            raise RuntimeError(
                "habr.cabinet: не нашёл data-token формы с "
                f"action={_CHANGE_EMAIL_START_URL!r} в HTML "
                f"/ru/cabinet. Фрагмент: {snippet!r}"
            )
        sitekey = _extract_cabinet_sitekey(html) or _HABR_FALLBACK_SITEKEY

        captcha = await solver.solve(_CABINET_URL, sitekey)
        if not captcha.success or not captcha.token:
            raise RuntimeError(
                "habr.cabinet/change-email/start: капча не решена: "
                f"{captcha.error}"
            )

        async with c.session.post(
            _CHANGE_EMAIL_START_URL,
            data={
                "email": new_email,
                "password": password,
                "smart-token": captcha.token,
            },
            headers={
                **_HABR_AJAX_HEADERS,
                "Referer": _CABINET_URL,
                "x-form-token": form_token,
                "Content-Type":
                    "application/x-www-form-urlencoded; charset=UTF-8",
            },
        ) as r:
            status = r.status
            try:
                data = await r.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                text = (await r.text())[:300]
                raise RuntimeError(
                    "habr.cabinet/change-email/start "
                    f"вернул не-JSON (status={status}): {text}"
                )

    ok = bool(data.get("success"))
    logger.info(
        "[habr.auth_flow] initiate_email_change(%s -> %s) ok=%s raw=%r",
        temp_email, new_email, ok, data,
    )
    if not ok:
        raise RuntimeError(
            f"habr.cabinet/change-email/start не успешен: {data!r}"
        )
    # ``demo_code`` пустой: реальный код прилетит юзеру в письмо
    # на ``new_email``, у нас его нет. Поле возвращаем, чтобы
    # orchestrator не упал на ``result["demo_code"]``.
    return {
        "ok": True,
        "raw": data,
        "endpoint": _CHANGE_EMAIL_START_URL,
        "demo_code": "",
    }


async def confirm_email_change(
    *,
    temp_email: str,
    new_email: str,
    code: str,
) -> dict[str, Any]:
    """Подтвердить смену email-а кодом из письма (``new_email``).

    HAR показал: на ``/finish`` капча не нужна, body содержит только
    ``code=NNNN``. ``x-form-token`` обязателен и регенерится habr-ом
    per-request, поэтому ПЕРЕД каждым ``/finish`` мы заново
    скачиваем ``/ru/cabinet`` и берём свежий токен.

    На неверном коде habr возвращает
    ``{"success":false,"errors":{"code":"Неверный или просроченный код"}}``
    — пробрасываем как ``RuntimeError("invalid_code: ...")``, который
    orchestrator смапит в ``HabrFlowError("invalid_code")`` для фронта.
    """
    async with HabrClient(email=temp_email) as c:
        form_token, _ = await _fetch_cabinet_meta(
            c, _CHANGE_EMAIL_FINISH_URL,
        )

        async with c.session.post(
            _CHANGE_EMAIL_FINISH_URL,
            data={"code": code},
            headers={
                **_HABR_AJAX_HEADERS,
                "Referer": _CABINET_URL,
                "x-form-token": form_token,
                "Content-Type":
                    "application/x-www-form-urlencoded; charset=UTF-8",
            },
        ) as r:
            status = r.status
            try:
                data = await r.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                text = (await r.text())[:300]
                raise RuntimeError(
                    "habr.cabinet/change-email/finish "
                    f"вернул не-JSON (status={status}): {text}"
                )

    ok = bool(data.get("success"))
    logger.info(
        "[habr.auth_flow] confirm_email_change(%s -> %s) ok=%s raw=%r",
        temp_email, new_email, ok, data,
    )
    if not ok:
        # habr на этом эндпоинте возвращает «invalid_code» как
        # ``errors.code = "Неверный или просроченный код"``.
        errors = data.get("errors") or {}
        err_blob = repr(data).lower()
        if (
            "code" in errors
            or "неверный" in err_blob
            or "просрочен" in err_blob
            or "invalid_code" in err_blob
            or "code.invalid" in err_blob
        ):
            raise RuntimeError(f"invalid_code: {data!r}")
        raise RuntimeError(
            f"habr.cabinet/change-email/finish не успешен: {data!r}"
        )
    return {
        "ok": True,
        "raw": data,
        "endpoint": _CHANGE_EMAIL_FINISH_URL,
    }


async def initiate_password_recovery(
    *,
    email: str,
) -> dict[str, Any]:
    """Запустить восстановление пароля по основному email юзера.

    Точное повторение asd.py:

    1. GET ``account.habr.com/`` → ``Location: /ru/ident/{token1}``.
    2. GET ``/ru/restore-password/{token1}`` → парсим ``data-sitekey``
       Yandex SmartCaptcha из HTML формы.
    3. Решаем капчу через 2Captcha (см. :class:`YandexCaptchaSolver`).
    4. POST ``/ru/restore-password/start/{token1}`` с ``email`` +
       ``smart-token``. Ответ ``{"success": true, "rurl":
       ".../restore-password/code/{token2}"}`` — извлекаем ``token2``.
    5. habr отправляет 6-значный код на ``email``; этот код позже
       проверит :func:`confirm_password_recovery` через ``token2``.

    Возвращает ``{"ok", "recovery_token": token2, "raw": data}``.
    ``recovery_token`` orchestrator кладёт в ``flow.extra``, откуда
    мы достанем его при подтверждении.
    """
    if not email:
        raise RuntimeError("initiate_password_recovery: пустой email")

    solver = YandexCaptchaSolver()
    async with aiohttp.ClientSession(
        headers=_common_headers(),
        timeout=_HTTP_TIMEOUT,
    ) as s:
        token1 = await _discover_ident_token(s)
        form_url = f"{ACCOUNT_BASE}/ru/restore-password/{token1}"
        sitekey = await _extract_sitekey(s, form_url)

        captcha = await solver.solve(form_url, sitekey)
        if not captcha.success or not captcha.token:
            raise RuntimeError(
                f"habr.restore-password: капча не решена: {captcha.error}"
            )

        async with s.post(
            f"{ACCOUNT_BASE}/ru/restore-password/start/{token1}",
            data={"email": email, "smart-token": captcha.token},
            headers={**_HABR_AJAX_HEADERS, "Referer": form_url},
        ) as r:
            try:
                data = await r.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                text = (await r.text())[:300]
                raise RuntimeError(
                    f"habr.restore-password/start вернул не-JSON "
                    f"(status={r.status}): {text}"
                )

    if not data.get("success"):
        raise RuntimeError(
            f"habr.restore-password/start не успешен: {data!r}"
        )

    rurl = data.get("rurl") or ""
    token2 = urlparse(rurl).path.rsplit("/", 1)[-1]
    if not token2:
        raise RuntimeError(
            f"habr.restore-password/start: не нашли token2 в rurl={rurl!r}"
        )

    logger.info(
        "[habr.auth_flow] initiate_password_recovery(%s) -> token2=%s…",
        email, token2[:8],
    )
    return {
        "ok": True,
        "recovery_token": token2,
        "raw": data,
        # Реальный 6-значный код приходит юзеру в письмо на ``email``;
        # мы его не знаем, поэтому ``demo_code`` пустой. Orchestrator
        # читает ``result["demo_code"]`` — пусть будет ключ.
        "demo_code": "",
    }


async def confirm_password_recovery(
    *,
    email: str,
    new_password: str,
    code: str,
    recovery_token: str | None = None,
) -> dict[str, Any]:
    """Завершить восстановление: проверить код + поставить новый пароль.

    Двухшаговая операция (как habr реально устроен):

    1. POST ``/ru/restore-password/check/{recovery_token}`` с
       ``{"code": code}`` — habr валидирует код. На неверном коде
       возвращает ``{"success": false, "code": "Неверный код"}``,
       тогда orchestrator поднимает ``HabrFlowError("invalid_code")``.
    2. POST ``/ru/restore-password/finish/{recovery_token}`` с
       ``{"password1": new_password, "password2": new_password}`` —
       habr ставит наш ``platform_password`` и аккаунт «легально»
       переходит к нам.

    Если код неверный (шаг 1) — поднимаем ``RuntimeError("invalid_code")``;
    вышестоящий orchestrator маппит его в HTTP-ошибку для фронта.
    Любая другая ошибка — RuntimeError с сырым ответом.
    """
    if not recovery_token:
        raise RuntimeError(
            "confirm_password_recovery: нет recovery_token "
            "(initiate_password_recovery не вызывался?)"
        )

    referer = f"{ACCOUNT_BASE}/ru/restore-password/code/{recovery_token}"
    async with aiohttp.ClientSession(
        headers=_common_headers(),
        timeout=_HTTP_TIMEOUT,
    ) as s:
        # Шаг 1: валидируем код.
        async with s.post(
            f"{ACCOUNT_BASE}/ru/restore-password/check/{recovery_token}",
            data={"code": code},
            headers={**_HABR_AJAX_HEADERS, "Referer": referer},
        ) as r:
            try:
                check_data = await r.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                text = (await r.text())[:300]
                raise RuntimeError(
                    f"habr.restore-password/check не-JSON "
                    f"(status={r.status}): {text}"
                )

        if not check_data.get("success"):
            err = check_data.get("code") or check_data
            logger.info(
                "[habr.auth_flow] confirm_password_recovery: "
                "check failed (%s): %r", email, err,
            )
            raise RuntimeError(f"invalid_code: {err!r}")

        # Шаг 2: ставим новый пароль.
        async with s.post(
            f"{ACCOUNT_BASE}/ru/restore-password/finish/{recovery_token}",
            data={
                "password1": new_password,
                "password2": new_password,
            },
            headers={**_HABR_AJAX_HEADERS, "Referer": referer},
        ) as r:
            try:
                finish_data = await r.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                text = (await r.text())[:300]
                raise RuntimeError(
                    f"habr.restore-password/finish не-JSON "
                    f"(status={r.status}): {text}"
                )

    if not finish_data.get("success"):
        raise RuntimeError(
            f"habr.restore-password/finish не успешен: {finish_data!r}"
        )

    logger.info(
        "[habr.auth_flow] confirm_password_recovery(%s) -> ok", email,
    )
    return {"ok": True, "check": check_data, "finish": finish_data}


# ── Orchestrator (вызывается из app/api/dashboard.py) ───────────────────


class HabrFlowError(RuntimeError):
    """Бизнес-ошибка habr-флоу, которую нужно поднять до API-слоя.

    ``code`` маппится в HTTPException.detail и понятен фронту:

    * ``main_email_required`` — у юзера нет основного email в резюме.
    * ``no_pending_flow``     — verify пришёл без предварительного connect.
    * ``invalid_code``        — введённый код не совпадает с ожидаемым.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


async def start_habr_connect_flow(
    *,
    user_id: uuid.UUID,
    main_email: str | None,
    temp_email: str | None,
    platform_password: str,
) -> dict[str, Any]:
    """Запустить флоу подключения habr.

    Возвращает payload, который ``/api/settings/platforms/connect``
    отдаёт фронту: ``flow_type``, ``main_email``, ``code_length``,
    ``demo_code`` (только в режиме заглушек).
    """
    if not main_email:
        raise HabrFlowError(
            "main_email_required",
            "У пользователя нет основного email в резюме — habr "
            "не сможет подтвердить владение аккаунтом. Попросите "
            "пользователя указать email в резюме.",
        )
    if not temp_email:
        # Без временной почты не получится зарегистрировать аккаунт
        # для ветки А. Сообщаем выше — обычно решается прогоном
        # ``ensure_platform_credentials`` ещё до вызова сюда.
        raise HabrFlowError(
            "temp_email_unavailable",
            "Сервис временной почты недоступен. Повторите подключение "
            "позже.",
        )

    exists = await check_habr_email_exists(main_email, platform_password)

    if not exists:
        # ── Ветка А: регистрируемся на temp-почту, потом меняем email ──
        await register_habr_account(
            temp_email=temp_email,
            password=platform_password,
        )
        result = await initiate_email_change(
            temp_email=temp_email,
            new_email=main_email,
            password=platform_password,
        )
        flow = HabrConnectFlow(
            user_id=user_id,
            flow_type="email_change",
            main_email=main_email,
            expected_code=result["demo_code"],
            extra={"temp_email": temp_email},
        )
    else:
        # ── Ветка Б: восстанавливаем пароль по основному email ─────────
        result = await initiate_password_recovery(email=main_email)
        flow = HabrConnectFlow(
            user_id=user_id,
            flow_type="password_recovery",
            main_email=main_email,
            expected_code=result["demo_code"],
            extra={"recovery_token": result.get("recovery_token")},
        )

    _PENDING_FLOWS[user_id] = flow
    logger.info(
        "[habr.auth_flow] flow started: user=%s type=%s main_email=%s",
        user_id, flow.flow_type, main_email,
    )

    return {
        "flow_type": flow.flow_type,
        "main_email": main_email,
        "code_length": len(flow.expected_code),
        # demo_code — только в режиме заглушек; UI его покажет в
        # подсказке под полем ввода, чтобы можно было руками
        # завершить флоу без реальной почты habr.
        "demo_code": flow.expected_code,
    }


async def verify_habr_connect_flow(
    *,
    user_id: uuid.UUID,
    code: str,
    platform_password: str,
) -> dict[str, Any]:
    """Подтвердить код, завершить habr-флоу.

    Возвращает словарь с финальным ``flow_type`` (для UX-сообщения).
    На реальной интеграции возвращаемое значение можно расширить —
    например, отдавать наружу свежий blob сессии, чтобы вызывающая
    сторона положила его в ``platform_credentials.encrypted_session``.
    """
    flow = _PENDING_FLOWS.get(user_id)
    if flow is None:
        raise HabrFlowError("no_pending_flow")

    code_clean = (code or "").strip()

    # В реальном флоу ``expected_code`` пустой — код знает только
    # habr (пришёл юзеру в письмо), мы его не угадаем. Локальную
    # верификацию делаем ТОЛЬКО если в флоу действительно прописан
    # demo-код (например, при unit-тестах или для других площадок,
    # которые могли бы сюда же лечь). Иначе — сразу к habr-у,
    # сервер сам ответит «success/false + Неверный код».
    if flow.expected_code and code_clean != flow.expected_code:
        raise HabrFlowError("invalid_code")

    try:
        if flow.flow_type == "email_change":
            await confirm_email_change(
                temp_email=flow.extra.get("temp_email", ""),
                new_email=flow.main_email,
                code=code,
            )
        else:
            await confirm_password_recovery(
                email=flow.main_email,
                new_password=platform_password,
                code=code,
                recovery_token=flow.extra.get("recovery_token"),
            )
    except RuntimeError as e:
        # confirm_email_change / confirm_password_recovery маркируют
        # «неверный код» через ``RuntimeError("invalid_code: ...")``
        # — мапим на бизнес-код, чтобы фронт корректно показал
        # «Неверный код» вместо общего «Не удалось подтвердить».
        if str(e).startswith("invalid_code"):
            raise HabrFlowError("invalid_code") from e
        raise

    _PENDING_FLOWS.pop(user_id, None)
    logger.info(
        "[habr.auth_flow] flow finished: user=%s type=%s",
        user_id, flow.flow_type,
    )

    # На этом этапе habr-аккаунт юзера уже либо переехал на основной
    # email (ветка email_change), либо его пароль был сброшен на наш
    # ``platform_password`` (ветка password_recovery). Сессионных
    # cookies, привязанных к user_id, при этом ЕЩЁ нет — все предыдущие
    # шаги работали либо на временных aiohttp-сессиях, либо через
    # in-memory OTP-bridge под ключом ``temp_email``. Чтобы воркер
    # сразу мог обращаться к career.habr.com без переподключения
    # площадки, делаем полный логин под итоговой парой
    # ``(main_email, platform_password)`` и сохраняем blob cookies в
    # ``platform_credentials.encrypted_session`` через
    # ``HabrClient.__aexit__``. Ошибка финального логина не должна
    # рушить весь флоу: credential exchange уже зафиксирован у habr,
    # а воркер при первом проходе сам перелогинится через
    # ``ensure_authenticated_or_relogin``.
    try:
        async with HabrClient(
            email=flow.main_email, user_id=user_id,
        ) as client:
            logged_in = await client.full_login(
                email=flow.main_email,
                password=platform_password,
            )
            if logged_in:
                logger.info(
                    "[habr.auth_flow] session persisted: user=%s",
                    user_id,
                )
                # После confirm_email_change на career-аккаунте всё
                # ещё лежит исходный никнейм, выведенный из local-part
                # временной почты регистрации. Если юзер потом
                # переподключит площадку, register/start под новой
                # временной почтой может отвалиться с
                # "Никнейм недоступен для регистрации", потому что
                # habr найдёт старый аккаунт с таким же ником. Сразу
                # после успешного логина приводим никнейм к
                # стабильному, выводимому из user_id, формату.
                if flow.flow_type == "email_change":
                    try:
                        new_nick = derive_career_nickname(user_id)
                        nick_ok = await client.change_career_nickname(
                            new_nick,
                        )
                        logger.info(
                            "[habr.auth_flow] post-email-change "
                            "nickname rotate ok=%s (user=%s, "
                            "new_nick=%s)",
                            nick_ok, user_id, new_nick,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "[habr.auth_flow] change_career_nickname "
                            "упал: %s (user=%s) — оставляю старый "
                            "никнейм, повторное подключение может "
                            "получить 'Никнейм недоступен'",
                            exc, user_id,
                        )
            else:
                logger.warning(
                    "[habr.auth_flow] финальный логин не прошёл, "
                    "encrypted_session не сохранён (user=%s) — "
                    "воркер сделает re-login сам", user_id,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[habr.auth_flow] финальный логин упал с исключением: %s "
            "(user=%s) — воркер сделает re-login сам", exc, user_id,
        )

    # Бридж под временным ключом теперь не нужен — миграция в БД
    # уже произошла через ``__aexit__``, а cookies из branch=email_change
    # были привязаны к ``temp_email`` и потеряли смысл.
    temp_email = flow.extra.get("temp_email")
    if temp_email:
        from app.services.job_sites.habr import _OTP_BRIDGE
        _OTP_BRIDGE.pop(temp_email, None)

    return {"ok": True, "flow_type": flow.flow_type}


def cancel_habr_connect_flow(user_id: uuid.UUID) -> bool:
    """Сбросить активное флоу (юзер нажал «Отмена» или «Отключить»)."""
    return _PENDING_FLOWS.pop(user_id, None) is not None


def get_pending_flow(user_id: uuid.UUID) -> HabrConnectFlow | None:
    """Полезно для отладки / возможной персистентности в будущем."""
    return _PENDING_FLOWS.get(user_id)
