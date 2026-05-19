"""
hh.ru OTP login client — асинхронный, с сохранением и переиспользованием сессии.

Зависимости:
    pip install aiohttp 2captcha-python

Использование (полный авто-логин):
    async with HHClient(phone="8AAABBBCCDD", user_id=user.id) as client:
        await client.ensure_logged_in()
        # дальше — любые приватные запросы через client.session / client.xhr_headers

Хранение сессии:
    * Если ``user_id`` задан — cookies (де)сериализуются из/в столбец
      ``platform_credentials.encrypted_session`` (см.
      :mod:`app.db.platform_credentials`).
    * Если ``user_id`` не задан (период между ``/auth/request-code``
      и ``/auth/verify-code``, когда юзера в БД ещё нет) — cookies
      живут во внутреннем in-memory словаре ``_OTP_BRIDGE``,
      ключ — hh-телефон. Это временный «бридж» на время OTP-флоу;
      pickle-файлов на диск больше не пишем.

Миграция OTP-сессии в БД:
    После успешного ``verify-code`` (юзер уже создан в БД) обычный
    путь — присвоить ``client.user_id = db_user.id`` ВНУТРИ блока
    ``async with``: тогда ``__aexit__`` сам положит cookies в БД и
    очистит in-memory бридж. См. :func:`migrate_otp_bridge_to_db`
    если нужно сделать это без активного клиента.
"""
from __future__ import annotations

import asyncio
import logging
import pickle
import tempfile
import uuid
from dataclasses import dataclass, field
from typing import Optional

import aiohttp
from aiohttp import ClientSession, CookieJar, FormData
from twocaptcha import TwoCaptcha
from yarl import URL
from bs4 import BeautifulSoup
import re

from app.services.job_sites.base import BaseJobSiteClient
from app.services.job_sites.hh.resume_parser import resume_parse
from app.services.job_sites.registry import PLATFORM_HH
from app.schemas.resume import Resume
from app.config import TWOCAPTCHA_KEY
from app.services.job_sites.hh.vacancy_extractor import *
from app.services.cover_letter import generate_cover_letter
from app.services.logos import download_company_logo
from app.db.session import session_scope
from app.db.models.vacancy_responses import VacancyResponse
from app.db.models.resume import Resume
from app.db.models.user import User
from app.db.platform_credentials import mark_platform_expired
from app.services.realtime import notify_response_change

from sqlalchemy import func, select, update
import random


logger = logging.getLogger(__name__)

# ── In-memory "бридж" между ``/auth/request-code`` и ``/auth/verify-code`` ─
# В этот момент юзера в БД ещё нет (его создают только после успешной
# проверки кода), а cookies hh.ru (``_xsrf``, ``__ddg*``) нужно
# переживать между двумя HTTP-запросами. Раньше для этого использовался
# pickle-файл на диске; теперь — словарь в памяти процесса.
#
# Ограничения:
#   * При рестарте uvicorn'а словарь обнуляется → юзеру придётся снова
#     дёрнуть ``/auth/request-code``. Это приемлемо: OTP флоу занимает
#     минуту-две, рестарты деплоя редки.
#   * При запуске uvicorn с ``--workers N`` (N > 1) бридж не разделяется
#     между процессами. В текущем масштабе у проекта одиночный воркер;
#     если понадобится горизонталь — поднимется Redis (см. roadmap в
#     app/queue/__init__.py).
_OTP_BRIDGE: dict[str, bytes] = {}


async def migrate_otp_bridge_to_db(hh_phone: str, user_id: uuid.UUID) -> bool:
    """Перенести OTP-сессию из in-memory бриджа в ``platform_credentials``.

    Используется как fallback в редком сценарии, когда после
    ``verify-code`` не удалось дойти до ``async with HHClient(...)``
    внутри которого мы переключаем ``user_id``. Возвращает ``True``,
    если что-то реально перенесли.
    """
    blob = _OTP_BRIDGE.pop(hh_phone, None)
    if blob is None:
        return False
    # Используем save_session с base-уровня, чтобы шифрование шло в
    # одном месте. HHClient-инстанс нужен только как «носитель slug'а».
    placeholder = HHClient(phone=hh_phone, user_id=user_id)
    await placeholder.save_session(user_id=user_id, blob=blob)
    return True

# ── Утилита: нормализация ссылок на резюме hh.ru ────────────────────────────
# Все три формы — «полный URL карточки», «converter-URL», «голый хэш» —
# приходят и из настроек юзера, и из ``get_resume_id()``. Один общий
# регексп вытаскивает только сам идентификатор; всё остальное —
# `?...` query-параметры, тип ответа, домен — собирается обратно в
# `get_resume(...)`. Хэш hh.ru — это hex-строка длиной ровно 38
# символов; но на старых аккаунтах встречались и более короткие
# (например, при первичной регистрации). Берём с запасом — 20+ hex.
_HH_RESUME_HASH_FROM_PATH_RE = re.compile(r"/resume/([0-9a-fA-F]+)")
_HH_RESUME_HASH_FROM_QUERY_RE = re.compile(r"[?&]hash=([0-9a-fA-F]+)")
_HH_RESUME_BARE_HASH_RE = re.compile(r"^[0-9a-fA-F]{20,}$")


def _extract_hh_resume_hash(value: str) -> str:
    """Достать hh-hash резюме из любой поддерживаемой формы.

    Поддерживаемые входы:

    * ``"<hash>"`` — уже готовый хэш (hex, минимум 20 символов);
    * ``"https://hh.ru/resume/<hash>"`` — карточка резюме;
    * ``"https://hh.ru/resume_converter/resume.txt?hash=<hash>&type=txt"`` —
      converter-URL;
    * любая другая ссылка с ``/resume/<hash>`` или ``?hash=<hash>`` в
      пути/query.

    Поднимает :class:`ValueError`, если ни одна форма не подошла —
    вызывающий код должен это поймать и вернуть 400/parse-error
    юзеру.
    """
    v = (value or "").strip()
    if not v:
        raise ValueError("empty resume hash/url")
    if _HH_RESUME_BARE_HASH_RE.match(v):
        return v
    m = _HH_RESUME_HASH_FROM_PATH_RE.search(v)
    if m:
        return m.group(1)
    m = _HH_RESUME_HASH_FROM_QUERY_RE.search(v)
    if m:
        return m.group(1)
    raise ValueError(f"can't extract resume hash from value: {v!r}")


# ── Константы ───────────────────────────────────────────────────────────────
BASE = "https://hh.ru"
USER_AGENT = (
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:150.0) "
    "Gecko/20100101 Firefox/150.0"
)
OK_KEY = "CODE_SEND_OK"
MAX_CAPTCHA_RETRIES = 4  # 2captcha на русской hh.ru-капче ошибается часто


# ── Исключения ──────────────────────────────────────────────────────────────
class HHAuthError(Exception):
    """Любая ошибка процесса авторизации.

    Кидается, когда сохранённая в БД сессия невалидна, а возможности
    автоматически переавторизоваться нет (мы намеренно не делаем
    интерактивный OTP-флоу из фонового кода — юзер должен сам нажать
    «переподключить» в ``/settings``). Параллельно с этим
    ``ensure_logged_in`` ставит ``platform_credentials.status='expired'``,
    чтобы UI показал бейдж и кнопку.
    """


# —— Утилиты ───────────────────────────────────────────────────────
def _to_hh_phone(e164: str) -> str:
    """+79XXXXXXXXX → 89XXXXXXXXX (формат, ожидаемый hh.ru)."""
    digits = e164.lstrip("+")
    if digits.startswith("7"):
        return "8" + digits[1:]
    return digits


# ── Клиент ──────────────────────────────────────────────────────────────────
@dataclass
class HHClient(BaseJobSiteClient):
    """Клиент площадки hh.ru.

    Регистрируется автоматически благодаря наследованию от
    ``BaseJobSiteClient`` и классовому атрибуту ``slug`` (см.
    :mod:`app.services.job_sites.base`). Воркер достаёт класс
    через ``get_client_class("hh")``.

    Поля:
        phone: hh-телефон в формате ``8XXXXXXXXXX``.
        user_id: UUID юзера в нашей БД. Если задан — cookies
            (де)сериализуются из/в ``platform_credentials
            .encrypted_session``. Если ``None`` — cookies живут
            в in-memory ``_OTP_BRIDGE`` (краткое окно между
            ``/auth/request-code`` и ``/auth/verify-code``,
            когда юзера в БД ещё нет).
    """

    # Идентификатор площадки. Этого хватает, чтобы базовый класс
    # автоматически положил ``HHClient`` в общую мапу клиентов.
    slug = PLATFORM_HH

    phone: str
    user_id: uuid.UUID | None = None

    # внутреннее состояние
    session: ClientSession = field(init=False, repr=False)
    _solver: TwoCaptcha = field(init=False, repr=False)
    _xsrf: Optional[str] = field(init=False, default=None, repr=False)

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def __aenter__(self) -> "HHClient":
        jar = CookieJar(quote_cookie=False)  # hh.ru cookies содержат '+', '/', '='
        await self._restore_cookies_into(jar)

        self.session = ClientSession(
            cookie_jar=jar,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self._solver = TwoCaptcha(
            TWOCAPTCHA_KEY, defaultTimeout=180, pollingInterval=5
        )
        return self

    async def __aexit__(self, *exc):
        # Сохраняем cookies при выходе, если в jar есть хоть что-то
        # осмысленное (логин выставляет hhrole/hhuid; hhtoken может и не
        # появиться при OTP-логине пустого аккаунта).
        try:
            if any(True for _ in self.session.cookie_jar):  # noqa
                await self._persist_cookies()
        except Exception as e:
            logger.warning("не удалось сохранить cookies: %s", e)
        await self.session.close()

    # ── сохранение / восстановление сессии ──────────────────────────────────
    #
    # Раньше был метод ``save_session()`` (синхронный, писал pickle на
    # диск). Теперь логика спрятана в двух хелперах ниже + наследованных
    # из ``BaseJobSiteClient`` ``save_session`` / ``load_session`` (они
    # знают про БД и шифрование).

    def _cookies_to_blob(self) -> bytes:
        """Pickle-байты текущего ``cookie_jar``. Тот же формат, что
        был у pickle-файла на диске."""
        return pickle.dumps(list(self.session.cookie_jar))

    @staticmethod
    def _blob_to_cookies(blob: bytes) -> list:
        return pickle.loads(blob)

    async def _restore_cookies_into(self, jar: CookieJar) -> None:
        """Заливает cookies в свежий ``CookieJar`` из БД или бриджа."""
        # 1. БД (canonical). Если есть user_id и blob — берём оттуда.
        if self.user_id is not None:
            try:
                blob = await self.load_session(user_id=self.user_id)
            except Exception as e:  # network / decrypt error
                logger.warning("load_session из БД упал: %s", e)
                blob = None
            if blob:
                try:
                    for cookie in self._blob_to_cookies(blob):
                        jar.update_cookies({cookie.key: cookie})
                    logger.info("cookies загружены из БД (user_id=%s)", self.user_id)
                    return
                except Exception as e:
                    logger.warning("не удалось разобрать blob из БД: %s", e)

        # 2. In-memory bridge — используется между request-code/verify-code,
        #    когда user_id ещё неизвестен.
        bridge_blob = _OTP_BRIDGE.get(self.phone)
        if bridge_blob:
            try:
                for cookie in self._blob_to_cookies(bridge_blob):
                    jar.update_cookies({cookie.key: cookie})
                logger.info("cookies загружены из in-memory бриджа")
            except Exception as e:
                logger.warning("не удалось разобрать blob из бриджа: %s", e)

    async def _persist_cookies(self) -> None:
        """Сохраняет cookies туда, где их потом ожидаем найти.

        * ``user_id`` задан → в БД (плюс очищаем in-memory бридж по
          этому телефону, чтобы он не задерживался дольше нужного).
        * ``user_id`` пустой → в in-memory бридж под ключом
          ``self.phone``.
        """
        blob = self._cookies_to_blob()
        if self.user_id is not None:
            await self.save_session(user_id=self.user_id, blob=blob)
            # OTP-бридж уже не нужен, чистим, чтобы он не "торчал" в RAM
            _OTP_BRIDGE.pop(self.phone, None)
            logger.debug("cookies сохранены в БД (user_id=%s)", self.user_id)
        else:
            _OTP_BRIDGE[self.phone] = blob
            logger.debug("cookies сохранены в in-memory бридж (phone=%s)", self.phone)

    @classmethod
    def from_context(cls, ctx):
        """Фабрика для воркера: получает ``user_id`` и ``phone_number``
        из :class:`WorkerContext` и собирает клиента."""
        if not ctx.phone_number:
            raise ValueError("не передан номер телефона")
        hh_phone = _to_hh_phone(ctx.phone_number)
        return cls(phone=hh_phone, user_id=ctx.user_id)


    # ── публичный API ───────────────────────────────────────────────────────
    async def ensure_logged_in(self) -> None:
        """Проверить кэш сессии. Без интерактивного re-login'а.

        Поведение
        ---------
        * Сессия валидна → ротируем ``_xsrf`` и возвращаемся. Дальше
          можно дёргать приватные эндпоинты.
        * Сессия невалидна → помечаем ``platform_credentials.status``
          юзера как ``'expired'`` и кидаем :class:`HHAuthError`. Юзер
          увидит в UI бейдж «переподключи» и сам запустит OTP-флоу из
          ``/settings``.

        Раньше тут стоял ``await self.login()`` — полный OTP-флоу с
        ``input()`` в консоль. В серверном процессе этот ``input``
        просто вис, забивая воркер на бесконечный таймаут. Поэтому
        интерактивный re-login из background-кода удалён намеренно
        и не подлежит восстановлению — OTP-флоу есть только в
        web-сценарии через ``begin_otp_login`` / ``finish_otp_login``,
        которые дёргает фронт по кнопке.
        """
        if await self.is_authenticated():
            logger.info("сессия из кэша валидна")
            # is_authenticated мог сделать GET / — он ротирует _xsrf.
            # Подтягиваем свежий, чтобы первый XHR не получил 403.
            self._xsrf = self._cookie("_xsrf")
            return

        logger.warning(
            "hh: сессия отсутствует/истекла, помечаю credentials как "
            "expired (user=%s)", self.user_id,
        )
        # Помечать имеет смысл, только если у клиента вообще есть
        # привязка к юзеру в БД. Кейс ``user_id is None`` — это OTP-
        # бридж между ``request-code`` и ``verify-code``; туда мы
        # сюда не попадём из обычных воркеров, но руки в карманы
        # держим аккуратно.
        if self.user_id is not None:
            await mark_platform_expired(
                user_id=self.user_id, platform="hh",
            )
        raise HHAuthError("hh_session_expired")

    # ── двухходовой OTP-флоу для веб-сценария ───────────────────────────────
    # В обычном login() и SMS-отправка, и ввод кода происходят в одном вызове
    # через интерактивный provider. В веб-приложении это два разных HTTP-
    # запроса, поэтому мы разделяем шаги. Между запросами cookies переживают
    # через ``_OTP_BRIDGE`` (in-memory), а после ``verify-code`` мигрируют
    # в БД (см. ``__aexit__`` → ``_persist_cookies``).
    async def begin_otp_login(self) -> dict:
        """
        Шаг 1 веб-логина: чистим протухшее, греем _xsrf, отправляем SMS.
        Возвращает информацию о посланном коде (форматированный номер,
        длина кода, через сколько секунд можно запросить повторно).
        """
        self._reset_stale_cookies()
        await self._warm_up()
        otp = await self._send_sms()
        # явный persist — на случай нештатного выхода до __aexit__
        await self._persist_cookies()
        return {
            "formatted_phone": otp["otp"]["formattedPhone"],
            "code_length": otp["codeLength"],
            "next_send_in": otp["otp"]["secondsUntilNextSend"],
        }

    async def complete_otp_login(self, code: str) -> None:
        """
        Шаг 2 веб-логина: отправляет введённый код. Перед вызовом ожидается,
        что cookies загружены из бриджа (это делает ``__aenter__``).
        """
        if not self._cookie("_xsrf"):
            raise HHAuthError(
                "нет _xsrf в загруженной сессии — сначала вызовите begin_otp_login"
            )
        await self._submit_code(code)
        self._xsrf = self._cookie("_xsrf")
        await self._persist_cookies()

    async def is_authenticated(self) -> bool:
        """
        Реальная проверка сессии — дёргаем приватную страницу и смотрим
        на код ответа.

        Залогиненный соискатель → 200.
        Аноним → 302 на /account/login (или 404, hh.ru ведёт себя по-разному
                                          в зависимости от User-Agent).
        Серверная сессия истекла → то же, что аноним, даже если в jar
                                    остался cookie hhrole=applicant.

        Поэтому проверка по cookie ненадёжна — спрашиваем сам сервер.
        """
        # Если в jar нет даже hhrole — точно аноним, экономим запрос
        role = (self._cookie("hhrole") or "").lower()
        if not role or role == "anonymous":
            logger.info("is_authenticated: hhrole=%r → False (без запроса)", role)
            return False

        # Реальный запрос: hh.ru при истёкшей сессии отдаёт 302/404, при
        # живой — 200. Используем browser_headers, иначе DataDome ругается.
        try:
            async with self.session.get(
                f"{BASE}/applicant/resumes",
                headers=self.browser_headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                logger.info("is_authenticated: GET /applicant/resumes → %s", r.status)
                if r.status in (301, 302):
                    loc = r.headers.get("Location", "")
                    logger.info("is_authenticated: redirect → %s", loc)
                    return "login" not in loc.lower()
                return r.status == 200
        except Exception as e:
            logger.warning("is_authenticated: запрос упал: %s", e)
            return False

    @property
    def xhr_headers(self) -> dict:
        """
        Заголовки для приватных XHR-запросов.
        ВАЖНО: _xsrf читается из cookie jar при каждом обращении —
        hh.ru ротирует его на ряде запросов (главная, /account/login/*),
        и закэшированное значение устареет → 403.
        """
        xsrf = self._cookie("_xsrf") or self._xsrf
        if not xsrf:
            raise HHAuthError("нет cookie _xsrf — сначала вызовите ensure_logged_in()")
        # Кэшируем последнее увиденное значение, чтобы _send_sms/etc.
        # могли работать до первого Set-Cookie ответа.
        self._xsrf = xsrf
        return {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-Xsrftoken": xsrf,
            "Origin": BASE,
            "Referer": f"{BASE}/?hhtmFrom=account_login",
            "X-hhtmSource": "main",
            "X-hhtmFrom": "account_login",
        }

    def xhr_headers_vacancy(self, vacancy_id) -> dict:
        """
        Заголовки для приватных XHR-запросов.
        ВАЖНО: _xsrf читается из cookie jar при каждом обращении —
        hh.ru ротирует его на ряде запросов (главная, /account/login/*),
        и закэшированное значение устареет → 403.
        """
        xsrf = self._cookie("_xsrf") or self._xsrf
        if not xsrf:
            raise HHAuthError("нет cookie _xsrf — сначала вызовите ensure_logged_in()")
        # Кэшируем последнее увиденное значение, чтобы _send_sms/etc.
        # могли работать до первого Set-Cookie ответа.
        self._xsrf = xsrf
        return {
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-Xsrftoken": xsrf,
            "Origin": BASE,
            "Referer": f"{BASE}/vacancy/{vacancy_id}",
            "X-hhtmSource": "vacancy"
        }

    @property
    def browser_headers(self) -> dict:
        """
        Заголовки для GET HTML-страниц (резюме, вакансии, поиск).
        Минимальный «браузерный» набор — БЕЗ X-Requested-With, X-Xsrftoken,
        Origin и прочих XHR-меток. С такими заголовками hh.ru/DataDome
        видит обычный переход по ссылке.
        """
        return {
            "Accept": ("text/html,application/xhtml+xml,application/xml;"
                       "q=0.9,image/avif,image/webp,*/*;q=0.8"),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-User": "?1",
        }

    async def fetch_html(
        self,
        url: str,
        *,
        referer: Optional[str] = None,
        retry_login: bool = True,
    ) -> str:
        """
        GET HTML-страницы с правильными заголовками.

        Если страница приватная и сервер возвращает 401/403/404/302→login,
        автоматически делаем повторный логин и ретраим запрос (один раз).
        """
        logger.info(f'start "fetch_html(url={url})"')
        headers = self.browser_headers
        if referer:
            headers["Referer"] = referer

        async with self.session.get(url, headers=headers,
                                    allow_redirects=False) as r:
            status = r.status
            text = await r.text(errors="replace")
            location = r.headers.get("Location", "")

        # Признаки «сессия мертва»
        looks_logged_out = (
            status in (401, 403, 404)
            or (status in (301, 302) and "login" in location.lower())
        )

        if looks_logged_out and retry_login:
            logger.warning(
                "GET %s → %s (location=%r) — похоже, сессия истекла, "
                "перелогиниваюсь", url, status, location)
            await self.login()   
            return await self.fetch_html(url, referer=referer,
                                         retry_login=False)

        if status >= 400:
            preview = text[:500].replace("\n", " ")
            raise HHAuthError(
                f"GET {url} → {status}. "
                f"Body preview: {preview!r}. "
                "Если это HTML с DataDome challenge — нужен curl_cffi."
            )

        if status in (301, 302):
            # Не приватный редирект — следуем вручную
            logger.info("GET %s → %s, follow %s", url, status, location)
            return await self.fetch_html(location, referer=url,
                                         retry_login=False)

        return text


    def form_respond_to_vacancy(self, vacancy_id: str, resume_hash: str, cover_letter: str) -> aiohttp.FormData:

        form = aiohttp.FormData()
        form.add_field("_xsrf", self._cookie("_xsrf"))
        form.add_field("vacancy_id", vacancy_id)
        form.add_field("resume_hash", resume_hash)
        form.add_field("ignore_postponed", "true")
        form.add_field("incomplete", "false")
        form.add_field("mark_applicant_visible_in_vacancy_country", "false")
        form.add_field("country_ids", [])
        form.add_field("letter", cover_letter)
        form.add_field("lux", "true")
        form.add_field("withoutTest", "no")
        form.add_field("hhtmFromLabel", "")
        form.add_field("hhtmSourceLabel", "")

        return form

    async def respond_to_vacancy(
        self,
        vacancy_id: str,
        resume_hash: str,
        cover_letter: str
    ) -> dict:

        async with self.session.post(f"{BASE}/shards/vacancy/register_interaction",
                                     json={"vacancyId": vacancy_id},
                                     headers=self.xhr_headers) as r:
            logger.debug(f"status of \"{BASE}/shards/vacancy/register_interaction\": {r.status}")
            #logger.debug(f"response of \"{BASE}/shards/vacancy/register_interaction\": {await r.json()}")

        async with self.session.get(f"{BASE}/applicant/vacancy_response/popup?vacancyId={vacancy_id}&isTest=no&withoutTest=no&lux=true&alreadyApplied=false") as vacancy_info_response:
            logger.debug(f"vacancy_info_response.status = {vacancy_info_response.status}")
            if vacancy_info_response.status == 200:
                parameters = await vacancy_info_response.json()
                logger.debug(f"vacancy_info_response parameters = {parameters}")
                
                response_type = parameters.get("type")
                if response_type == "test-required":
                    async with self.session.get(f"{BASE}{parameters.get('redirect_uri')}") as page_questions_r:
                        if page_questions_r.status == 200:

                            soup = BeautifulSoup(await page_questions_r.text(), "html.parser")
                            req_data: dict = {}

                            # 1) hidden поля
                            wanted = {"uidPk", "guid", "startTime", "testRequired", "_xsrf"}
                            for inp in soup.find_all("input", attrs={"type": "hidden"}):
                                name = inp.get("name")
                                if name in wanted:
                                    req_data[name] = inp.get("value", "")

                            missing = wanted - req_data.keys()
                            if missing:
                                raise ValueError(f"Не нашёл hidden-поля: {missing}")

                            task_name_re = re.compile(r"^task_\d+_text$")
                            radio_name_re = re.compile(r"^task_\d+$")
                            # 2) tasks: пробегаем по блокам task-body, в каждом ищем вопрос + textarea
                            questions: list[dict] = []
                            for body in soup.select('[data-qa="task-body"]'):
                                question_el = body.select_one('[data-qa="task-question"]')
                                question = question_el.get_text(strip=True) if question_el else ""

                                # 2a) текстовый вопрос: textarea
                                textarea = body.find("textarea", attrs={"name": task_name_re})
                                if textarea:
                                    questions.append(
                                        {
                                            "type": "text",
                                            "field": textarea["name"],  # task_276129145_text
                                            "question": question,
                                            "options": None,
                                        }
                                    )
                                    continue

                                # 2b) радио-вопрос: набор <input type="radio" name="task_<id>" value="...">
                                radios = body.find_all("input", attrs={"type": "radio", "name": radio_name_re})
                                if radios:
                                    field = radios[0]["name"]  # task_345521486
                                    options: list[dict] = []
                                    for r in radios:
                                        # текст варианта обычно лежит в ближайшем data-qa="cell-text-content"
                                        # ищем в общем родителе <label> (cell), куда входит этот radio
                                        label = r.find_parent("label")
                                        text_el = (label or body).select_one('[data-qa="cell-text-content"]')
                                        # на случай если у одного radio свой label, а text_el оказался первым в body —
                                        # подстрахуемся: ищем именно внутри label, иначе fallback
                                        if label is not None:
                                            text_el = label.select_one('[data-qa="cell-text-content"]') or text_el
                                        options.append(
                                            {
                                                "value_id": r.get("value", ""),  # 345521487
                                                "text": text_el.get_text(strip=True) if text_el else "",
                                                # "От 120 000 ..."
                                            }
                                        )
                                    questions.append(
                                        {
                                            "type": "radio",
                                            "field": field,
                                            "message": question,
                                            "options": options,
                                        }
                                    )
                                    continue

                            # Fallback: если по какой-то причине у hh нет .task-body (старая разметка)
                            # — просто соберём textarea вне привязки к вопросам.
                            if not questions:
                                raise "Ошибка чтения дополнительных вопросов"

                            req_data["questions"] = questions

                            form = self.form_respond_to_vacancy(vacancy_id, resume_hash, cover_letter)
                            form.add_field("uidPk", req_data.get("uidPk"))
                            form.add_field("guid", req_data.get("guid"))
                            form.add_field("startTime", req_data.get("startTime"))
                            form.add_field("testRequired", req_data.get("testRequired"))

                            for question in req_data["questions"]:
                                #answer = ask_ai(question.get("message"))
                                answer = "не могу точно сказать" # генерация ии ответов на вопросы
                                form.add_field(question.get("field"), answer)

                            headers = self.xhr_headers
                        else:
                            raise f"page_questions_r.status = {page_questions_r.status}"

                elif response_type in ["quickResponse", "modal"]:

                    form = self.form_respond_to_vacancy(vacancy_id, resume_hash, cover_letter)

                    #data_gecko_format = self.generate_geckoformboundary(data)
                    logger.debug(f"data_gecko_format = {form}") # noqa
                    print(self.xhr_headers_vacancy(vacancy_id))

                    headers = self.xhr_headers_vacancy(vacancy_id)
                    
                else:
                    raise_response = f"Незивестный ответ по запросу {vacancy_info_response.url}"
                    raise raise_response

                async with self.session.post(f"{BASE}/applicant/vacancy_response/popup",
                                             data=form,
                                             headers=headers,
                                             allow_redirects=False) as vacancy_response:

                    logger.debug(f"vacancy_response.status = {vacancy_response.status}")
                    ans = await vacancy_response.json()
                    logger.debug(f"vacancy_response.json() = {ans}")
                    if vacancy_response.status == 200:
                        result = await vacancy_response.json()
                        return result

        return {"error": "unknown"}

    # ── шаги логина ─────────────────────────────────────────────────────────
    async def _warm_up(self) -> None:
        """GET главной страницы — забираем _xsrf и cookies от DataDome."""
        async with self.session.get(BASE) as r:
            await r.read()
        self._xsrf = self._cookie("_xsrf")
        if not self._xsrf:
            raise HHAuthError(
                "_xsrf не получен — вероятно, заблокировал DataDome. "
                "Попробуйте curl_cffi/Playwright."
            )

    async def _send_sms(self) -> dict:
        """
        Probe-запрос → если капча не нужна, SMS уже отправлено.
        Если нужна — решаем капчу, при ошибке ретраим до MAX_CAPTCHA_RETRIES.
        """
        otp = await self._otp_generate()
        hhc = otp.get("hhcaptcha") or {}

        if not (hhc.get("isBot") and hhc.get("captchaState")):
            self._assert_sms_sent(otp)
            return otp

        captcha_state = hhc["captchaState"]

        for attempt in range(1, MAX_CAPTCHA_RETRIES + 1):
            logger.info("капча, попытка %d/%d", attempt, MAX_CAPTCHA_RETRIES)
            captcha_id: Optional[str] = None
            try:
                captcha_key = await self._request_captcha_key()
                logger.info("captchaKey=%s, скачиваю картинку", captcha_key)
                image = await self._download_captcha(captcha_key)
                logger.info("картинка %d байт → 2captcha "
                            "(до 3 минут, прогресс не виден)", len(image))
                captcha_text, captcha_id = await self._solve_captcha(image)
                logger.info("✓ 2captcha вернул: %r", captcha_text)

                otp = await self._otp_generate(captcha={
                    "key": captcha_key,
                    "text": captcha_text,
                    "state": captcha_state,
                })
            except Exception:
                # Любая сетевая ошибка между получением капчи и ответом —
                # помечаем капчу как невалидную (мы её, по сути, не использовали)
                if captcha_id:
                    await self._report_captcha(captcha_id, False)
                raise

            new_hhc = otp.get("hhcaptcha") or {}
            captcha_failed = bool(new_hhc.get("captchaError"))

            # Один report на одну решённую капчу
            if captcha_id:
                await self._report_captcha(captcha_id, not captcha_failed)

            if not captcha_failed:
                self._assert_sms_sent(otp)
                return otp

            # сервер прислал новый state для следующей попытки
            captcha_state = new_hhc.get("captchaState") or captcha_state
            logger.warning("hh.ru отверг капчу, пробую заново "
                           "(осталось попыток: %d)",
                           MAX_CAPTCHA_RETRIES - attempt)

        raise HHAuthError(
            f"капча не пройдена за {MAX_CAPTCHA_RETRIES} попыток — "
            "проверьте баланс/качество 2captcha или попробуйте позже"
        )

    async def _submit_code(self, code: str) -> dict:
        form = FormData()
        form.add_field("username", self.phone)
        form.add_field("code", code)
        form.add_field("remember", "true")
        form.add_field("accountType", "APPLICANT")
        form.add_field("isApplicantSignup", "true")
        form.add_field("operationType", "otp_auth")
        form.add_field("backurl", f"{BASE}/")

        async with self.session.post(
            f"{BASE}/account/login/by_code",
            data=form,
            headers=self.xhr_headers,
        ) as r:
            r.raise_for_status()
            data = await r.json()

        ver = data.get("verification") or {}
        if not data.get("success") or not ver.get("success"):
            raise HHAuthError(f"логин не прошёл: {ver.get('key') or data}")
        return data

    # ── низкоуровневые HTTP-обёртки ─────────────────────────────────────────
    async def _otp_generate(self, captcha: Optional[dict] = None) -> dict:
        form = FormData()
        form.add_field("_xsrf", self._xsrf)
        form.add_field("backurl", f"{BASE}/")
        form.add_field("isSignupPage", "")
        form.add_field("operationType", "applicant_otp_auth")
        form.add_field("formatPhone", "true")
        form.add_field("login", self.phone)
        if captcha:
            form.add_field("captchaKey", captcha["key"])
            form.add_field("captchaText", captcha["text"])
            form.add_field("captchaState", captcha["state"])

        async with self.session.post(
            f"{BASE}/account/otp_generate",
            data=form,
            headers=self.xhr_headers,
        ) as r:
            r.raise_for_status()
            return await r.json()

    async def _request_captcha_key(self) -> str:
        async with self.session.post(
            f"{BASE}/captcha",
            params={"lang": "RU"},
            headers={
                **self.xhr_headers,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=b"",
        ) as r:
            r.raise_for_status()
            return (await r.json())["key"]

    async def _download_captcha(self, key: str) -> bytes:
        async with self.session.get(
            f"{BASE}/captcha/picture", params={"key": key}
        ) as r:
            r.raise_for_status()
            return await r.read()

    # ── 2captcha (sync lib → executor) ──────────────────────────────────────
    async def _solve_captcha(self, image: bytes) -> tuple[str, str]:
        with tempfile.NamedTemporaryFile(suffix='.png', delete=True) as tmp:
            tmp.write(image)
            tmp.flush()  # убеждаемся, что данные попали на диск
            # tmp.name — уникальный путь, например /tmp/tmp1a2b3c.png
            result = await asyncio.to_thread(
                self._solver.normal,
                str(tmp.name),
                phrase=1, numeric=2, caseSensitive=0, # noqa
                lang="ru", # noqa
                hintText="Введите два слова, разделённые пробелом", # noqa
            )
        return result["code"], result["captchaId"]

    async def _report_captcha(self, captcha_id: str, correct: bool) -> None:
        try:
            await asyncio.to_thread(self._solver.report, captcha_id, correct)
        except Exception as e:
            logger.warning("report 2captcha не удался: %s", e)

    # ── утилиты ─────────────────────────────────────────────────────────────
    def _cookie(self, name: str) -> Optional[str]:
        for c in self.session.cookie_jar: # noqa
            if c.key == name:
                return c.value
        return None

    def _reset_stale_cookies(self) -> None:
        """
        Перед полным логином чистим всё, что относится к авторизации
        и DataDome. `hhuid` оставляем — он идентифицирует браузер,
        и его сохранение между сессиями выглядит для антибота естественно.
        """
        from http.cookies import Morsel # noqa
        keep_names = {"hhuid"}

        # Снимаем значения тех cookies, что хотим сохранить
        preserved: dict[str, str] = {}
        all_names: list[str] = []
        for c in list(self.session.cookie_jar): # noqa
            all_names.append(c.key)
            if c.key in keep_names:
                preserved[c.key] = c.value

        if not all_names:
            return

        dropped = [n for n in all_names if n not in keep_names]
        logger.info("сбрасываю cookies: %s (сохраняю %s)",
                    sorted(set(dropped)), sorted(preserved.keys()))

        self.session.cookie_jar.clear()

        # Возвращаем preserved обратно
        if preserved:
            morsels = {}
            for name, value in preserved.items():
                m: Morsel = Morsel()
                m.set(name, value, value)
                morsels[name] = m
            self.session.cookie_jar.update_cookies(
                morsels, response_url=URL(BASE)
            )
        # сбрасываем кэш _xsrf — куки больше нет
        self._xsrf = None


    @staticmethod
    def _assert_sms_sent(otp: dict) -> None:
        if not otp.get("success") or otp.get("key") != OK_KEY:
            raise HHAuthError(f"SMS не отправлен: {otp}")

    @staticmethod
    def parse_vacancy_ids(html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        ids: list[str] = []
        seen: set[str] = set()

        # Each vacancy card is rendered as a wrapper with data-qa="vacancy-serp__vacancy"
        # containing a <div id="<vacancyId>" class="vacancy-card--..."> element.
        for card in soup.select('[data-qa="vacancy-serp__vacancy"]'):
            inner = card.find("div", id=re.compile(r"^\d+$"),
                              class_=re.compile(r"vacancy-card"))
            if inner is None:
                continue
            vid = str(inner.get("id"))
            if vid and vid not in seen:
                seen.add(vid)
                ids.append(vid)

        return ids


    async def get_resume_id(self) -> dict:
        # Используем fetch_html — он сам ставит правильные заголовки
        # и автоматически перелогинится при 401/403/404
        logger.info('START "get_resume_id()"')
        html = await self.fetch_html(f"{BASE}/applicant/resumes")
        logger.info(f'END "fetch_html(url={f"{BASE}/applicant/resumes"})"')
        soup = BeautifulSoup(html, "html.parser")

        name = ((soup.find(attrs={"data-qa": "profile-activator-fullname"}) or soup.find("h1") or soup.find(
            "title")))
        if name:
            name = name.get_text(strip=True)
            resume_ids = [str(a["href"]).split("resume/")[-1].split("?")[0] for a in soup.find_all("a", href=True) if "/resume/" in a["href"] and "edit/" not in a["href"]]
            if not resume_ids:
                return {"success": False, "error": "resume is not found"}
            elif len(resume_ids) > 1:
                return {"success": False, "error": "number of resume more than one", "resume_ids": [resume_ids]}
            else:
                return {"success": True, "user_name": name, "resume_id": resume_ids[0]}
        else:
            raise f"Не найдено имя на странице \"{BASE}/applicant/resumes\""


    async def get_resume(self, resume_hash_or_url: str) -> Resume:
        """Скачать ``resume.txt`` для одного резюме hh.ru и распарсить.

        Принимает на вход что угодно из:

        * ``"<32+ hex symbols>"`` — голый hh-hash резюме;
        * ``"https://hh.ru/resume/<hash>"`` — публичная ссылка на резюме;
        * ``"https://hh.ru/resume_converter/resume.txt?hash=<hash>&type=txt"`` —
          конечный converter-URL.

        Любая из форм нормализуется к
        ``https://hh.ru/resume_converter/resume.txt?hash=<hash>&type=txt``
        (этот эндпоинт hh.ru отдаёт «плейн-текстовое» резюме, удобное для
        парсинга — см. :func:`resume_parse`). Без свежей сессии hh.ru
        отдаст 403 — это обработает вызывающий код (``HHClient`` проверяет
        ``is_authenticated`` перед вызовом).
        """
        resume_hash = _extract_hh_resume_hash(resume_hash_or_url)
        url = f"{BASE}/resume_converter/resume.txt?hash={resume_hash}&type=txt"
        async with self.session.get(url) as resume_html_r:
            if resume_html_r.status == 200:
                resume_html = await resume_html_r.text()
                return resume_parse(resume_html)
            raise RuntimeError(
                f"resume_converter returned status {resume_html_r.status} "
                f"for hash {resume_hash}"
            )


    # ── Чатик hh.ru (chatik.hh.ru) ───────────────────────────────────────
    #
    # API чатов живёт на отдельном поддомене ``chatik.hh.ru``. Cookies
    # (``_xsrf``, ``hhtoken``, ``hhuid``, ``_my_session``) проставляются
    # на ``.hh.ru`` и автоматически уходят сюда же благодаря
    # ``CookieJar(unsafe=True)`` (см. ``BaseJobSiteClient``).
    CHATIK_BASE = "https://chatik.hh.ru"
    CHATIK_PER_PAGE = 20  # размер страницы; крутим, пока items >= 20

    @property
    def chatik_headers(self) -> dict:
        """Заголовки для XHR-запросов к ``chatik.hh.ru``."""
        xsrf = self._cookie("_xsrf") or self._xsrf
        if not xsrf:
            raise HHAuthError(
                "нет cookie _xsrf — сначала вызовите ensure_logged_in()"
            )
        self._xsrf = xsrf
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
            "X-Xsrftoken": xsrf,
            "Origin": BASE,
            "Referer": f"{BASE}/chatik",
            "X-hhtmSource": "chatik",
        }

    async def list_chats_page(
        self,
        *,
        page: int = 0,
        filter_unread: bool = False,
        filter_has_text_message: bool = False,
    ) -> dict:
        """Один запрос к ``chatik.hh.ru/chatik/api/chats``.

        Возвращает распарсенный JSON (см. формат в ``chats.json``):
        корневые ключи ``chats`` (``items[]``, ``page``, ``hasNextPage``),
        ``chatsDisplayInfo`` (заголовок/подзаголовок/иконка по chatId),
        ``resources`` (детали вакансий/работодателей/...).
        """
        url = (
            f"{self.CHATIK_BASE}/chatik/api/chats"
            f"?filterUnread={'true' if filter_unread else 'false'}"
            f"&filterHasTextMessage="
            f"{'true' if filter_has_text_message else 'false'}"
            f"&page={page}"
        )
        async with self.session.get(url, headers=self.chatik_headers) as r:
            if r.status == 401 or r.status == 403:
                raise HHAuthError(
                    f"chatik returned {r.status} — сессия истекла, нужен OTP"
                )
            r.raise_for_status()
            return await r.json(content_type=None)

    async def list_all_chats(
        self,
        *,
        filter_unread: bool = False,
        filter_has_text_message: bool = False,
        max_pages: int = 100,
    ) -> list[dict]:
        """Собрать все чаты, листая страницы по 20 элементов.

        Идём, пока на странице ровно ``CHATIK_PER_PAGE`` (=20) элементов —
        как только пришло меньше, останавливаемся. ``max_pages`` —
        страховка от бесконечного цикла, реально на типовом аккаунте
        чатов сильно меньше тысячи.
        """
        items: list[dict] = []
        page = 0
        while page <= max_pages:
            try:
                payload = await self.list_chats_page(
                    page=page,
                    filter_unread=filter_unread,
                    filter_has_text_message=filter_has_text_message,
                )
            except HHAuthError:
                raise
            except Exception:
                logger.exception("chatik /chats?page=%s failed", page)
                break
            chats_block = payload.get("chats") or {}
            page_items = chats_block.get("items") or []
            # Прикрепляем displayInfo прямо в item, чтобы синку было
            # удобнее: ходить по двум структурам сразу не нужно.
            display_info = payload.get("chatsDisplayInfo") or {}
            for it in page_items:
                di = display_info.get(str(it.get("id"))) or {}
                it["_displayInfo"] = di
                items.append(it)
            if len(page_items) < self.CHATIK_PER_PAGE:
                break
            page += 1
        return items

    async def mark_chat_read(
        self,
        *,
        chat_id: str,
        message_id: str,
        has_unread_discard: bool = False,
    ) -> dict:
        """POST ``chatik.hh.ru/chatik/api/mark_read`` — пометить чат прочитанным.

        Параметры тела (JSON):
            * ``chatId`` (int) — id чата на hh.ru;
            * ``messageId`` (int) — id последнего просмотренного сообщения
              (берётся из ``lastViewedByCurrentUserMessageId`` или из
              ``lastMessage.id`` как fallback);
            * ``hasUnreadDiscardMessage`` (bool) — есть ли непрочитанный
              системный «отказ» в чате (для UI hh — флажок «корзина»);
            * ``hasUnseenLocationInconsistencyBotMessage`` (bool) — не наш
              кейс, всегда ``false``.

        Возвращает распарсенный JSON (обычно ``{}`` или статус).
        """
        url = f"{self.CHATIK_BASE}/chatik/api/mark_read"
        payload = {
            "chatId": int(chat_id),
            "messageId": int(message_id),
            "hasUnreadDiscardMessage": bool(has_unread_discard),
            "hasUnseenLocationInconsistencyBotMessage": False,
        }
        async with self.session.post(
            url, headers=self.chatik_headers, json=payload
        ) as r:
            if r.status in (401, 403):
                raise HHAuthError(
                    f"chatik mark_read {r.status} — сессия истекла"
                )
            r.raise_for_status()
            try:
                return await r.json(content_type=None)
            except Exception:
                return {}

    async def send_chat_message(
        self,
        *,
        chat_id: str,
        text: str,
        idempotency_key: str | None = None,
    ) -> dict:
        """POST ``chatik.hh.ru/chatik/api/send`` — отправить сообщение в чат.

        Тело (JSON):
            * ``chatId`` (int) — id чата на hh.ru;
            * ``idempotencyKey`` (str) — UUID4, генерируется на сервере;
              hh использует его для дедупликации повторного отправления;
            * ``text`` (str) — сам текст сообщения.

        Если ``idempotency_key`` не передан — сгенерируется свежий UUID4.
        Возвращает распарсенный JSON ответа hh — там приходит сам
        сохранённый message-объект (``{message: {id, ...}}``).
        """
        url = (
            f"{self.CHATIK_BASE}/chatik/api/send"
            "?hhtmSourceLabel=chat&hhtmSource=chat_hh_page"
        )
        payload = {
            "chatId": int(chat_id),
            "idempotencyKey": idempotency_key or str(uuid.uuid4()),
            "text": text,
        }
        async with self.session.post(
            url, headers=self.chatik_headers, json=payload
        ) as r:
            if r.status in (401, 403):
                raise HHAuthError(
                    f"chatik send {r.status} — сессия истекла"
                )
            r.raise_for_status()
            try:
                return await r.json(content_type=None)
            except Exception:
                return {}

    async def get_chat_data(
        self,
        *,
        chat_id: str,
        applicant_id: str,
    ) -> dict:
        """GET ``chatik.hh.ru/chatik/api/chat_data?chatId=…&applicantId=…``.

        Возвращает полные данные диалога: всю историю сообщений (а не
        только последнее, как ``/chats``), участников, статусы. Парсер
        этого ответа — в :func:`app.services.job_sites.hh.chats_sync.extract_chat_data_messages`.
        """
        url = (
            f"{self.CHATIK_BASE}/chatik/api/chat_data"
            f"?chatId={chat_id}"
            f"&applicantId={applicant_id}"
            f"&do_not_track_session_events=false"
        )
        async with self.session.get(url, headers=self.chatik_headers) as r:
            if r.status in (401, 403):
                raise HHAuthError(
                    f"chatik chat_data {r.status} — сессия истекла"
                )
            r.raise_for_status()
            return await r.json(content_type=None)

    async def parse_vacancies(self, resume_hash: str) -> list[str]:
        logger.info(f'START "parse_vacancies({resume_hash})"')
        page_vacancies = await self.fetch_html(f"{BASE}/search/vacancy?ored_clusters=true&resume={resume_hash}&order_by=publication_time")
        result = self.parse_vacancy_ids(page_vacancies)
        logger.info(f'END "parse_vacancies({resume_hash})"')
        return result

    async def parse_vacancy(self, vacancy_id: int) -> Vacancy:
        async with self.session.get(f"{BASE}/vacancy/{vacancy_id}") as vacancy_response:
            vacancy = extract_vacancy(await vacancy_response.text())
            vacancy.link=f"{BASE}/vacancy/{vacancy_id}"
            return vacancy

    async def respond_to_vacancies(self, ctx) -> dict:
        """Откликнуться на все релевантные вакансии под резюме юзера.

        Объединяет за один проход всё, что нужно сделать воркеру для одной
        итерации hh-площадки:

            1. Заходит на hh.ru от лица юзера (берёт сохранённые cookies или
            логинится через OTP при необходимости).
            2. Достаёт ``resume_id`` юзера из его кабинета.
            3. Через поиск hh.ru по резюме собирает список релевантных
            вакансий.
            4. Для каждой вакансии — парсит карточку, для pro-юзеров
            генерирует AI cover-letter, отправляет отклик.

        Параметры
        ---------
        ctx : WorkerContext
            Снимок юзера: ``phone_number``, ``user_id``, тариф, ``resume_txt``
            и т.п. См. ``app.workers.context.WorkerContext``.

        Возвращает
        ----------
        dict с ключами:

        * ``scraped_vacancies``       — сколько вакансий удалось распарсить.
        * ``cover_letters_generated`` — сколько AI cover-letter'ов
        сгенерировано (всегда 0 для не-pro-тарифов).
        * ``responses``               — список ответов hh.ru на каждый
        POST отклика (включая ошибки, для подсчёта и отладки).
        """
        scraped_vacancies = 0
        cover_letters_generated = 0
        responses: list[dict] = []

        async with HHClient.from_context(ctx) as client:
            try:
                await client.ensure_logged_in()
            except HHAuthError:
                # ``ensure_logged_in`` уже пометил
                # ``platform_credentials.status='expired'`` — UI
                # покажет юзеру, что hh нужно переподключить вручную.
                # Тихо возвращаем «ничего не сделали», воркер не падает.
                logger.warning(
                    "respond_to_vacancies (hh): сессия невалидна, "
                    "пропускаю проход (user=%s)", ctx.user_id,
                )
                return {
                    "scraped_vacancies": scraped_vacancies,
                    "cover_letters_generated": cover_letters_generated,
                    "responses": responses,
                    "error": "hh_session_expired",
                }

            resume_id_response = await client.get_resume_id()
            if not resume_id_response.get("success"):
                logger.warning(
                    "respond_to_vacancies: get_resume_id failed: %s",
                    resume_id_response,
                )

                return {
                    "scraped_vacancies": scraped_vacancies,
                    "cover_letters_generated": cover_letters_generated,
                    "responses": responses,
                    "error": resume_id_response.get(
                        "error", "resume_id_unavailable"
                    ),
                }

            resume_id = resume_id_response.get("resume_id")
            logger.info("resume_id: %s", resume_id)

            vacancies = await client.parse_vacancies(resume_id)

            resume_txt = ctx.resume_txt
            if resume_txt is None:
                print("RESUME_TXT IS NONE")
            else:
                print("RESUME_TXT IS NOT NONE")
                resume_txt = (await client.get_resume(resume_id)).raw_text

            for vacancy_id in vacancies:
                vacancy = await client.parse_vacancy(vacancy_id)

                if vacancy:
                    scraped_vacancies += 1

                cover_letter = ""
                if ctx.is_pro:
                    cover_letter = await generate_cover_letter(
                        str(ctx.user_id),
                        resume_txt,
                        vacancy.as_text(),
                    )
                    cover_letters_generated += 1

                print(f"cover_letter: {cover_letter}")
                result = await client.respond_to_vacancy(
                    vacancy_id=vacancy_id,
                    resume_hash=resume_id,
                    cover_letter=cover_letter,
                )
                print(f"RESULT: {result}")
                responses.append(result)
                error = result.get("error")
                # Заранее генерим UUID, чтобы скачать логотип под
                # тем же именем файла, что и id строки. Альтернатива
                # — сначала INSERT и flush(), потом скачать и UPDATE,
                # но это два запроса вместо одного.
                vacancy_response_id = uuid.uuid4()
                logo_ext = await download_company_logo(
                    vacancy.company_logo,
                    vacancy_response_id,
                    session=client.session,
                )
                async with session_scope() as db:
                    vacancy_response = VacancyResponse(
                        id=vacancy_response_id,
                        user_id=ctx.user_id,
                        title=vacancy.title,
                        company=vacancy.company or None,
                        salary=vacancy.salary or None,
                        service='hh',
                        link=vacancy.link,
                        logo_ext=logo_ext,
                        is_sent=bool(not error),
                        error=error
                    )
                    db.add(vacancy_response)
                    await db.flush()
                    # Live-push на /responses: NOTIFY доедет до подписчиков
                    # после COMMIT'а ``session_scope``, и WebSocket-эндпоинт
                    # пушит свежий снапшот (карточка появится сверху).
                    await notify_response_change(db, ctx.user_id)
                    # Флаг ``auto_apply_running`` живёт
                    # теперь прямо на ``users``.
                    user_row = (
                        await db.execute(
                            select(User).where(User.id == ctx.user_id)
                        )
                    ).scalar_one_or_none()
                    if user_row is None or not user_row.auto_apply_running:
                        break

                await asyncio.sleep(random.randint(9, 12))

        return {
            "scraped_vacancies": scraped_vacancies,
            "cover_letters_generated": cover_letters_generated,
            "responses": responses,
        }