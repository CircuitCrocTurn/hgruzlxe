"""
superjob.ru — асинхронный клиент по принципу :mod:`HHClient` /
:class:`HabrClient`.

Зачем оно нужно
---------------

В отличие от hh.ru и Habr Career, на SuperJob у нас нет «штатного»
OTP-флоу. Сценарий, который описывает приложенный (исходный)
``superjob_bot.py``::

    1) Тянем «человеческое» резюме (город «Москва», уровень «Высшее»,
       занятость «Полная», и т. д.).
    2) Подтягиваем справочники SuperJob (``/dictionary/container``)
       и переводим текст → внутренние id, понятные API.
    3) Регистрируем нового соискателя
       (``POST /jsapi3/0.1/nodejsaccount/``) и создаём резюме
       (``POST /jsapi3/0.1/resume/``).
    4) Открываем ``/user/responses/`` → парсим «подходящие вакансии»
       → массово отправляем отклики
       (``POST /jsapi3/0.1/cvApplication/``) с настраиваемой паузой.

Адаптация для нашего проекта
----------------------------

* Источник «человеческого» резюме — БД (`Resume.parsed_data`,
  pydantic-схема :class:`app.schemas.resume.Resume`). YAML с диска
  больше не нужен — `_load_superjob_resume_input` собирает
  :class:`ResumeInput` из ``parsed_data`` (плюс ``users.email`` как
  fallback).
* HTTP — :mod:`aiohttp` (а не sync :mod:`requests`), чтобы
  соответствовать ``HHClient`` / ``HabrClient`` и не блокировать loop.
* Сессия (cookies + ``resume_id`` / ``applicant_id``) уходит в
  ``platform_credentials.encrypted_session`` через унаследованные
  ``save_session`` / ``load_session``. На повторном запуске воркер
  поднимает cookies из БД и не регистрирует юзера повторно.
* Воркер зовёт единственный метод :meth:`SuperJobClient.respond_to_vacancies`
  — он сам решает, регистрировать ли (если в БД пока нет
  ``resume_id``) и потом ходит по ``/user/responses/`` и шлёт
  отклики, пиша в ``vacancy_responses`` ровно так же, как делает
  ``HabrClient``.

``superjob_bot.py`` остаётся как «источник правды» для запросов
(каждый method класса — это HAR-копия одного из запросов скрипта),
но в продакшен-коде используется только этот клиент.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pickle
import random
import re
import string
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Tuple
from urllib.parse import quote

import aiohttp
from aiohttp import ClientResponseError, ClientSession, CookieJar
from sqlalchemy import select
from yarl import URL

from app.db.models.resume import Resume
from app.db.models.user import User
from app.db.models.vacancy_responses import VacancyResponse
from app.db.session import session_scope
from app.services.job_sites.base import BaseJobSiteClient
from app.services.job_sites.registry import PLATFORM_SUPERJOB
from app.services.logos import download_company_logo
from app.services.platform_password import generate_platform_password
from app.services.realtime import notify_response_change
from app.services.temp_mail import wait_for_link


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

BASE = "https://www.superjob.ru"
API = f"{BASE}/jsapi3/0.1"

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:121.0) "
    "Gecko/20100101 Firefox/121.0"
)

DEFAULT_RESPONSES_URL = f"{BASE}/user/responses/"
DEFAULT_PRIVACY_AGREEMENT = "1"

VACANCY_ID_RE = re.compile(r"f-test-vacancy-item-(\d+)")

# SuperJob не объявляет лимит как «1 в 10 секунд», но HAR-снимки
# показывают, что массовая отправка откликов спокойно идёт с паузой
# 10–14с; берём +- то же окно, что у habr.
VACANCY_RESPONSE_DELAY: Tuple[float, float] = (11.0, 14.0)


logger = logging.getLogger("superjob")


# ---------------------------------------------------------------------------
# In-memory bridge для до-БД сессий (по аналогии с HH/Habr).
# ---------------------------------------------------------------------------

_OTP_BRIDGE: dict[str, bytes] = {}


# ---------------------------------------------------------------------------
# Исключения
# ---------------------------------------------------------------------------


class AlreadyAppliedError(RuntimeError):
    """SuperJob ответил 409/403 на cvApplication — отклик уже был."""


class VacancyResponseError(RuntimeError):
    """HTTP-ошибка при отклике (400/403/422/др.)."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class RateLimitedError(VacancyResponseError):
    """SuperJob вернул 429 / прислал «слишком часто» — нужно подождать."""


class SuperJobAuthError(RuntimeError):
    """Не удалось ни поднять сессию из БД, ни зарегистрировать аккаунт."""


# ---------------------------------------------------------------------------
# Нормализация текста (для резолва справочников)
# ---------------------------------------------------------------------------

# Латинские буквы, которые в кириллических справочниках SJ иногда
# встречаются вместо своих визуально-идентичных кириллических
# аналогов (например, «Cвободное владение» с латинской «C»).
_LATIN_TO_CYRILLIC = str.maketrans(
    {
        "a": "а", "c": "с", "e": "е", "o": "о", "p": "р",
        "x": "х", "y": "у", "k": "к", "b": "в", "h": "н",
        "m": "м", "t": "т", "i": "и",
    }
)


def _normalize(value: str) -> str:
    """Универсальный «слаг» для сравнения справочников: lower + без
    диакритики + замена визуально похожих латинских букв на
    кириллические."""
    if not value:
        return ""
    s = unicodedata.normalize("NFKD", value).lower()
    s = re.sub(r"[ё]", "е", s)
    s = s.translate(_LATIN_TO_CYRILLIC)
    s = re.sub(r"[^a-zа-я0-9]+", " ", s)
    return s.strip()


def _random_session_id() -> str:
    """80 hex-символов — формат cookie ``_ws`` на сайте."""
    return uuid.uuid4().hex + uuid.uuid4().hex[:16]


def _random_email(domain: str = "example.com") -> str:
    local = "".join(
        random.choices(string.ascii_lowercase + string.digits, k=10)
    )
    return f"{local}@{domain}"


# ---------------------------------------------------------------------------
# DTO для входных данных (повторяют ``superjob_bot.ResumeInput``)
# ---------------------------------------------------------------------------


@dataclass
class ResumeExperience:
    position: str
    date_start: str          # YYYY-MM
    date_end: str | None     # YYYY-MM or None/"" → текущая работа
    company: str
    responsibility: str = ""
    town: str | None = None  # текст, по умолчанию = город соискателя


@dataclass
class ResumeEducation:
    level: str               # «Высшее», «Бакалавр» и т.п.
    institute: str           # название/аббревиатура для /institute/
    year_end: int | None = None
    faculty: str = ""
    specialty: str = ""
    form: str | None = None  # «Дневная/Очная», «Заочная»…


@dataclass
class ResumeLanguageInput:
    name: str                # «Английский язык»
    level: str               # «Свободное владение», «Разговорный»…


@dataclass
class ResumeInput:
    email: str
    first_name: str
    last_name: str
    birth_date: str          # YYYY-MM-DD
    position: str
    salary: int

    town: str = "Москва"
    work_type: str = "Полная"
    currency: str = "RUB"
    remote_work: bool = False
    business_trip: bool = True
    relocate_possibility: bool = False
    is_volunteer: bool = False
    moveable_towns: str = ""

    experience: list[ResumeExperience] = field(default_factory=list)
    education: list[ResumeEducation] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    languages: list[ResumeLanguageInput] = field(default_factory=list)

    citizenship: str | None = None
    driving_licenses: list[str] = field(default_factory=list)
    privacy_agreement: str = DEFAULT_PRIVACY_AGREEMENT


@dataclass
class ResolvedProfile:
    """Уже резолвленные значения, готовые к отправке на SJ API."""

    town_id: str
    work_type_id: str
    currency_id: str
    citizenship_id: str | None
    education_levels: dict[str, str] = field(default_factory=dict)
    education_forms: dict[str, str] = field(default_factory=dict)
    language_levels: dict[str, str] = field(default_factory=dict)
    languages: dict[str, str] = field(default_factory=dict)
    driving_licenses: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Снимок сессии (cookies + applicant_id + resume_id)
# ---------------------------------------------------------------------------


def _cookies_to_blob(
    jar: CookieJar,
    *,
    applicant_id: str | None,
    resume_id: str | None,
) -> bytes:
    """Сериализуем cookies и id-шники резюме одним blob'ом.

    Формат::

        {
            "cookies": pickle.dumps(list(cookie_jar)),
            "applicant_id": str | None,
            "resume_id":    str | None,
        }
    """
    payload = {
        "cookies": pickle.dumps(list(jar)),
        "applicant_id": applicant_id,
        "resume_id": resume_id,
    }
    return pickle.dumps(payload)


def _blob_to_cookies(
    blob: bytes,
) -> tuple[list, str | None, str | None]:
    """Обратное преобразование. Поддерживает legacy-формат
    ``pickle.dumps(list(jar))`` без applicant/resume id."""
    payload = pickle.loads(blob)
    if isinstance(payload, dict) and "cookies" in payload:
        cookies = pickle.loads(payload["cookies"])
        return (
            list(cookies),
            payload.get("applicant_id"),
            payload.get("resume_id"),
        )
    # legacy
    if isinstance(payload, list):
        return payload, None, None
    raise ValueError(
        f"superjob session blob: неизвестный формат {type(payload).__name__}"
    )


# ---------------------------------------------------------------------------
# Маппер из ``Resume.parsed_data`` в ``ResumeInput``
# ---------------------------------------------------------------------------


def _str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip() or default


def _parse_salary(value: Any) -> int:
    """«250 000 ₽» → 250000. ``None`` → 0."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else 0


_MONTH_NAMES_RU = {
    "январь": "01", "января": "01",
    "февраль": "02", "февраля": "02",
    "март": "03", "марта": "03",
    "апрель": "04", "апреля": "04",
    "май": "05", "мая": "05",
    "июнь": "06", "июня": "06",
    "июль": "07", "июля": "07",
    "август": "08", "августа": "08",
    "сентябрь": "09", "сентября": "09",
    "октябрь": "10", "октября": "10",
    "ноябрь": "11", "ноября": "11",
    "декабрь": "12", "декабря": "12",
}


def _parse_month_year(value: str | None) -> str | None:
    """«Декабрь 2022» / «12.2022» / «2022-12» → «2022-12».

    Возвращает ``None``, если месяц/год не распознали (тогда вызывающий
    код пропустит запись).
    """
    if not value:
        return None
    raw = str(value).strip().lower()
    if not raw or raw in ("настоящее время", "по настоящее время", "сейчас"):
        return None
    # YYYY-MM
    m = re.match(r"^(\d{4})-(\d{1,2})$", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    # MM.YYYY / MM/YYYY
    m = re.match(r"^(\d{1,2})[./](\d{4})$", raw)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    # YYYY → возьмём январь
    m = re.match(r"^(\d{4})$", raw)
    if m:
        return f"{m.group(1)}-01"
    # «Декабрь 2022»
    m = re.match(r"^([а-яё]+)\s+(\d{4})$", raw)
    if m:
        month_name, year = m.group(1), m.group(2)
        mm = _MONTH_NAMES_RU.get(month_name)
        if mm:
            return f"{year}-{mm}"
    return None


def _parse_birthday(value: str | None) -> str | None:
    """«12 ноября 2000» / «2000-11-12» → «2000-11-12»."""
    if not value:
        return None
    raw = str(value).strip().lower()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", raw)
    if m:
        return (
            f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        )
    m = re.match(r"^(\d{1,2})\s+([а-яё]+)\s+(\d{4})$", raw)
    if m:
        day, month_name, year = m.group(1), m.group(2), m.group(3)
        mm = _MONTH_NAMES_RU.get(month_name)
        if mm:
            return f"{year}-{mm}-{int(day):02d}"
    return None


def _split_full_name(value: str | None) -> tuple[str, str]:
    """«Иван Иванов» → («Иван», «Иванов»). Если фамилии нет —
    отдаём имя в обе позиции (SJ требует оба поля)."""
    if not value:
        return ("Соискатель", "Соискатель")
    parts = [p for p in re.split(r"\s+", str(value).strip()) if p]
    if not parts:
        return ("Соискатель", "Соискатель")
    if len(parts) == 1:
        return (parts[0], parts[0])
    # Привычный формат hh: «Фамилия Имя [Отчество]».
    return (parts[1], parts[0])


def _resume_input_from_parsed(
    *,
    user_email: str | None,
    parsed: dict[str, Any],
) -> ResumeInput | None:
    """Собираем :class:`ResumeInput` из ``Resume.parsed_data``.

    Возвращает ``None``, если данных принципиально не хватает
    (нет email и нет ФИО). Для отсутствующих опциональных
    полей подставляем безопасные дефолты — SuperJob API считает,
    что минимально нужно: ``email``, ``firstName``, ``lastName``,
    ``birthDate``, ``position``, ``salary``, ``town``, ``workType``,
    ``currency``.
    """
    first_name, last_name = _split_full_name(parsed.get("full_name"))

    # email: сначала parsed → потом из users → потом ничего
    email = ""
    for candidate in (parsed.get("email"), user_email):
        if candidate and "@" in str(candidate):
            email = str(candidate).strip()
            break
    if not email:
        # Без email регистрация невозможна. Возвращаем None — caller
        # отрапортует session_expired/missing_credentials.
        logger.warning(
            "[superjob] не удалось определить email "
            "(нет в parsed_data и в users.email) — клиент остановится"
        )
        return None

    birth_date = _parse_birthday(parsed.get("birthday")) or "1990-01-01"
    position = _str(parsed.get("title"), "")
    if not position:
        # SJ требует position. Если в резюме её нет — пробуем взять
        # первый skill, иначе подставляем безопасный fallback.
        skills = parsed.get("skills") or []
        position = (
            _str(skills[0]) if skills else "Специалист"
        )

    salary = _parse_salary(parsed.get("salary"))
    if not salary:
        salary = 50000

    town = _str(parsed.get("location"), "Москва")
    # ``location`` в hh-резюме часто выглядит как «Москва, метро …» —
    # SJ ищет город по подстроке, но безопаснее обрезать до первой
    # запятой.
    town = town.split(",", 1)[0].strip() or "Москва"

    employment = _str(parsed.get("employment_type"), "")
    if "полн" in employment.lower():
        work_type = "Полная"
    elif "част" in employment.lower():
        work_type = "Частичная занятость / совместительство"
    elif "стаж" in employment.lower():
        work_type = "Полная"
    else:
        work_type = "Полная"

    currency_raw = (parsed.get("currency") or "").strip().upper()
    currency_map = {"₽": "RUB", "RUB": "RUB", "USD": "USD", "$": "USD", "EUR": "EUR", "€": "EUR"}
    currency = currency_map.get(currency_raw, "RUB")

    work_format = _str(parsed.get("work_format"), "").lower()
    remote_work = "удал" in work_format or "remote" in work_format

    def _is_ready(value: str | None) -> bool:
        """``True`` если в строке есть «готов», но НЕ «не готов»."""
        if not value:
            return False
        norm = value.lower()
        if "готов" not in norm:
            return False
        # Грубая, но достаточная проверка: «не готов» / «не очень готов».
        return not re.search(r"не\s+\w*\s*готов", norm)

    relocate_possibility = _is_ready(parsed.get("relocation"))
    business_trip = _is_ready(parsed.get("business_trips"))

    experience: list[ResumeExperience] = []
    for raw in (parsed.get("experience") or []):
        if not isinstance(raw, dict):
            continue
        date_start = _parse_month_year(raw.get("start"))
        if not date_start:
            # без даты начала SJ откажет — пропускаем запись
            continue
        date_end_raw = raw.get("end")
        date_end = _parse_month_year(date_end_raw)
        # «настоящее время» → None (текущая работа)
        if date_end_raw and "настоящ" in str(date_end_raw).lower():
            date_end = None
        experience.append(
            ResumeExperience(
                position=_str(raw.get("position"), position),
                date_start=date_start,
                date_end=date_end,
                company=_str(raw.get("company"), "company"),
                responsibility=_str(raw.get("description"), ""),
                town=_str(raw.get("location"), "") or None,
            )
        )

    education: list[ResumeEducation] = []
    for raw in (parsed.get("education") or []):
        if not isinstance(raw, dict):
            continue
        if not raw.get("institution"):
            continue
        year = raw.get("year")
        try:
            year_end = int(re.sub(r"[^\d]", "", str(year))) if year else None
        except ValueError:
            year_end = None
        education.append(
            ResumeEducation(
                level=_str(raw.get("level"), "Высшее образование"),
                institute=_str(raw.get("institution"), ""),
                year_end=year_end,
                faculty=_str(raw.get("faculty"), ""),
                specialty="",
                form=None,
            )
        )

    languages_in: list[ResumeLanguageInput] = []
    for raw in (parsed.get("languages") or []):
        if isinstance(raw, dict):
            name = _str(raw.get("name"), "")
            level = _str(raw.get("level"), "Базовый")
            if name:
                languages_in.append(
                    ResumeLanguageInput(name=name, level=level)
                )
        elif isinstance(raw, str):
            parts = re.split(r"\s*[—\-:]\s*", raw, maxsplit=1)
            if parts and parts[0].strip():
                level = parts[1].strip() if len(parts) > 1 else "Базовый"
                languages_in.append(
                    ResumeLanguageInput(name=parts[0].strip(), level=level)
                )

    skills_list: list[str] = []
    for raw in (parsed.get("skills") or []):
        if raw:
            skills_list.append(str(raw))

    citizenship_list = parsed.get("citizenship") or []
    citizenship: str | None = None
    if isinstance(citizenship_list, list) and citizenship_list:
        citizenship = str(citizenship_list[0])
    elif isinstance(citizenship_list, str):
        citizenship = citizenship_list

    driving = parsed.get("driving") or {}
    driving_licenses: list[str] = []
    if isinstance(driving, dict):
        for cat in driving.get("categories") or []:
            driving_licenses.append(str(cat))

    return ResumeInput(
        email=email,
        first_name=first_name,
        last_name=last_name,
        birth_date=birth_date,
        position=position,
        salary=salary,
        town=town,
        work_type=work_type,
        currency=currency,
        remote_work=remote_work,
        business_trip=business_trip,
        relocate_possibility=relocate_possibility,
        is_volunteer=False,
        moveable_towns="",
        experience=experience,
        education=education,
        skills=skills_list,
        languages=languages_in,
        citizenship=citizenship,
        driving_licenses=driving_licenses,
    )


@dataclass(frozen=True, slots=True)
class _SuperJobBootstrap:
    """То, что нужно ``SuperJobClient`` для холодного старта.

    ``resume`` — собранный из ``Resume.parsed_data`` ``ResumeInput``.
    ``temp_email_token`` — токен от ``temp.coda.ink``, нужен для
    опроса inbox-а во время регистрации. ``None`` если у юзера ещё
    не выдан ``temp_email`` (тогда работаем без подтверждения).
    """

    resume: ResumeInput
    temp_email_token: str | None


async def _load_superjob_resume_input(
    user_id: uuid.UUID,
) -> _SuperJobBootstrap | None:
    """Достать резюме юзера из БД и сконвертировать в ``ResumeInput``.

    Если у юзера выдан ``temp_email`` (см.
    :func:`app.services.temp_mail.create_temp_email`) — используем его
    как login на SuperJob (это позволит позже забрать письмо с
    confirmation-ссылкой через ``users.temp_email_token``). Иначе —
    fallback на ``Resume.parsed_data['email']`` / ``users.email``.

    Возвращает ``None``, если резюме не распарсено или ключевых
    данных недостаточно (см. :func:`_resume_input_from_parsed`).
    """
    async with session_scope() as db:
        user = (
            await db.execute(select(User).where(User.id == user_id))
        ).scalar_one_or_none()
        if user is None:
            logger.warning(
                "[superjob] _load_superjob_resume_input: user=%s не найден",
                user_id,
            )
            return None
        resume_row = (
            await db.execute(
                select(Resume).where(Resume.user_id == user_id)
            )
        ).scalar_one_or_none()
        parsed: dict[str, Any] = {}
        if resume_row is not None and isinstance(resume_row.parsed_data, dict):
            parsed = dict(resume_row.parsed_data)
        # temp_email имеет приоритет: на него легче забрать письмо.
        if user.temp_email:
            parsed["email"] = user.temp_email
        user_email = user.email
        temp_email_token = user.temp_email_token

    resume = _resume_input_from_parsed(
        user_email=user_email,
        parsed=parsed,
    )
    if resume is None:
        return None
    return _SuperJobBootstrap(
        resume=resume,
        temp_email_token=temp_email_token,
    )


# ---------------------------------------------------------------------------
# Клиент
# ---------------------------------------------------------------------------


@dataclass
class SuperJobClient(BaseJobSiteClient):
    """Клиент площадки superjob.ru.

    Регистрируется автоматически благодаря наследованию от
    ``BaseJobSiteClient`` и классовому атрибуту ``slug`` (см.
    :mod:`app.services.job_sites.base`). Воркер достаёт класс
    через ``get_client_class("superjob")``.

    Поля:
        email: email юзера на SuperJob. Берётся из
            ``Resume.parsed_data['email']`` (fallback —
            ``users.email``). Используется как ключ ``_OTP_BRIDGE``
            и логин при ``POST /nodejsaccount/``.
        user_id: UUID юзера в нашей БД. Если задан — cookies +
            ``applicant_id`` / ``resume_id`` (де)сериализуются в
            ``platform_credentials.encrypted_session``.
    """

    slug = PLATFORM_SUPERJOB

    email: str
    user_id: uuid.UUID | None = None

    # внутреннее состояние
    session: ClientSession = field(init=False, repr=False)
    session_id: str | None = field(init=False, default=None, repr=False)
    applicant_id: str | None = field(init=False, default=None, repr=False)
    resume_id: str | None = field(init=False, default=None, repr=False)
    _dicts_cache: dict[str, list[dict[str, Any]]] | None = field(
        init=False, default=None, repr=False,
    )
    resolved: ResolvedProfile | None = field(
        init=False, default=None, repr=False,
    )

    # ── lifecycle ───────────────────────────────────────────────────
    async def __aenter__(self) -> "SuperJobClient":
        jar = CookieJar(quote_cookie=False)
        await self._restore_session_into(jar)

        self.session = ClientSession(
            cookie_jar=jar,
            headers={
                "User-Agent": DEFAULT_UA,
                "Accept-Language": "ru,en;q=0.9",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        )
        return self

    async def __aexit__(self, *exc):
        try:
            await self._persist_session()
        except Exception as e:
            logger.warning("[superjob] не удалось сохранить cookies: %s", e)
        await self.session.close()

    # ── сериализация сессии ─────────────────────────────────────────
    async def _restore_session_into(self, jar: CookieJar) -> None:
        """Заливает cookies и id-шники из БД (или in-memory bridge)
        в текущий клиент."""
        blob: bytes | None = None
        if self.user_id is not None:
            try:
                blob = await self.load_session(user_id=self.user_id)
            except Exception as e:  # network / decrypt
                logger.warning("[superjob] load_session из БД упал: %s", e)
        if blob is None:
            blob = _OTP_BRIDGE.get(self.email)

        if not blob:
            return
        try:
            cookies, applicant_id, resume_id = _blob_to_cookies(blob)
        except Exception as e:
            logger.warning("[superjob] разбор blob'а упал: %s", e)
            return
        for cookie in cookies:
            jar.update_cookies({cookie.key: cookie})
        if applicant_id:
            self.applicant_id = applicant_id
        if resume_id:
            self.resume_id = resume_id
        logger.info(
            "[superjob] сессия восстановлена (user_id=%s, resume_id=%s)",
            self.user_id, self.resume_id,
        )

    async def _persist_session(self) -> None:
        try:
            jar = self.session.cookie_jar  # type: ignore[union-attr]
        except AttributeError:
            return
        if not any(True for _ in jar):  # noqa
            return
        blob = _cookies_to_blob(
            jar,  # type: ignore[arg-type]
            applicant_id=self.applicant_id,
            resume_id=self.resume_id,
        )
        if self.user_id is not None:
            await self.save_session(user_id=self.user_id, blob=blob)
            _OTP_BRIDGE.pop(self.email, None)
        else:
            _OTP_BRIDGE[self.email] = blob

    # ── Фабрика для воркера ─────────────────────────────────────────
    @classmethod
    def from_context(cls, ctx) -> "SuperJobClient":
        """Создаёт инстанс из ``WorkerContext``.

        Идентификатор юзера — его email. Берём в порядке приоритета:

        1. ``ctx.prefs.profile_url`` уже не подходит (это hh-резюме);
           поэтому идём в БД к ``Resume.parsed_data['email']`` /
           ``users.email``. Здесь, в фабрике, у нас БД нет под рукой —
           SuperJob использует email как стабильный ключ
           ``_OTP_BRIDGE``, поэтому довольствуемся deterministic
           заглушкой на базе ``user_id``: если в реальном резюме
           будет email, он подхватится в ``__aenter__`` ↔
           ``respond_to_vacancies`` (там идём в БД).
        2. ``ctx.phone_number`` — только как ключ. SuperJob его как
           email не примет, но он стабильный → для bridge сойдёт,
           если юзер ещё не успел распарсить резюме.
        """
        email = (
            getattr(ctx, "superjob_email", None)
            or getattr(getattr(ctx, "prefs", None), "superjob_email", None)
            or f"sj-{ctx.user_id}@offerday.local"
        )
        return cls(email=email, user_id=ctx.user_id)

    # ── HTTP helpers ────────────────────────────────────────────────
    def _json_api_headers(
        self, *, referer: str, page_type: str,
    ) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Accept-Language": "ru,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": BASE,
            "Referer": referer,
            "X-Requested-With": "XMLHttpRequest",
            "x-frontend-project": "desktop",
            "x-page-type": page_type,
            "x-subdomain": "www",
            "x-requests-group-id": uuid.uuid4().hex,
        }

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None,
        referer: str,
        page_type: str,
    ) -> dict[str, Any]:
        url = f"{API}{path}"
        async with self.session.request(
            method,
            url,
            json=body,
            headers=self._json_api_headers(referer=referer, page_type=page_type),
        ) as r:
            text = await r.text()
            if r.status >= 400:
                logger.error(
                    "[superjob] %s %s -> %s: %s",
                    method, url, r.status, text[:300],
                )
                if r.status == 429:
                    raise RateLimitedError(r.status, text)
                if r.status in (401, 403, 409) and "/cvApplication" in path:
                    raise AlreadyAppliedError(text[:200])
                raise VacancyResponseError(r.status, text)
            if not text:
                return {}
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {}

    async def _get_html(
        self, url: str, *, referer: str | None = None,
    ) -> str:
        headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "ru,en;q=0.9",
        }
        if referer:
            headers["Referer"] = referer
        async with self.session.get(url, headers=headers) as r:
            r.raise_for_status()
            return await r.text()

    # ── Низкоуровневые API-зовы ─────────────────────────────────────
    async def warmup(self) -> None:
        """Тёплый запуск: получаем cookie ``_ws`` или генерим её сами."""
        try:
            await self._get_html(f"{BASE}/")
        except ClientResponseError as exc:
            logger.warning("[superjob] warmup: %s", exc)
        jar = self.session.cookie_jar  # type: ignore[union-attr]
        ws = None
        for cookie in jar:  # noqa
            if cookie.key == "_ws":
                ws = cookie.value
                break
        if not ws:
            ws = _random_session_id()
            jar.update_cookies({"_ws": ws}, response_url=URL(BASE))
            logger.info(
                "[superjob] _ws cookie не получен от сервера, "
                "сгенерировали локально %s…", ws[:16],
            )
        self.session_id = ws or _random_session_id()

    DICT_INCLUDE = (
        "maritalStatuses,children,languages,languageLevels,educationLevels,"
        "educationForms,drivingLicenses,citizenships,resumePublishedStatus,"
        "workTypes,resumeSelfEmployments,resumeNoWorkExperienceCauses,"
        "currencies"
    )

    async def _load_dictionaries(
        self,
    ) -> dict[str, list[dict[str, Any]]]:
        if self._dicts_cache is not None:
            return self._dicts_cache
        path = (
            f"/dictionary/container/?include={quote(self.DICT_INCLUDE, safe=',')}"
        )
        logger.info("[superjob] тяну справочники…")
        resp = await self._request_json(
            "GET", path,
            body=None,
            referer=f"{BASE}/resume/create/",
            page_type="resume-create",
        )
        by_type: dict[str, list[dict[str, Any]]] = {}
        for inc in resp.get("included", []) or []:
            by_type.setdefault(inc["type"], []).append(inc)
        self._dicts_cache = by_type
        return by_type

    async def _resolve_dict(
        self,
        type_name: str,
        query: str,
        *,
        attrs: Iterable[str] = ("defaultLabel", "key", "shortLabel"),
        required: bool = True,
    ) -> str | None:
        q = _normalize(query)
        if not q:
            return None
        items = (await self._load_dictionaries()).get(type_name, [])
        for it in items:
            for a in attrs:
                v = (it.get("attributes") or {}).get(a)
                if v is not None and _normalize(str(v)) == q:
                    return it["id"]
        for it in items:
            if it["id"].lower() == query.lower():
                return it["id"]
        for it in items:
            for a in attrs:
                v = (it.get("attributes") or {}).get(a)
                if v is not None and q in _normalize(str(v)):
                    return it["id"]
        if required:
            options = sorted(
                {
                    str((it.get("attributes") or {}).get("defaultLabel"))
                    for it in items
                    if (it.get("attributes") or {}).get("defaultLabel")
                }
            )[:30]
            raise SuperJobAuthError(
                f"не смог сопоставить «{query}» со справочником {type_name}. "
                f"Доступные значения (первые 30): {options}"
            )
        return None

    async def resolve_town(self, name: str) -> str:
        if not name:
            raise SuperJobAuthError("не задан город")
        path = (
            "/geo/?page[limit]=15&page[offset]=0&include=town"
            f"&filters[domain]=700&filters[keywords]={quote(name)}&filters[type]=town"
        )
        resp = await self._request_json(
            "GET", path,
            body=None,
            referer=f"{BASE}/resume/create/",
            page_type="resume-create",
        )
        for inc in resp.get("included", []) or []:
            if inc.get("type") == "town":
                return inc["id"]
        for d in resp.get("data") or []:
            if d.get("type") == "town":
                return d["id"]
            rel_town = (
                (d.get("relationships") or {}).get("town") or {}
            ).get("data")
            if rel_town and rel_town.get("type") == "town":
                return rel_town["id"]
        raise SuperJobAuthError(f"город «{name}» не найден на SuperJob")

    async def resolve_skill(
        self, profession: str, skill_name: str,
    ) -> str | None:
        path = (
            "/professionalSkill/?page[limit]=15&page[offset]=0"
            f"&filters[profession]={quote(profession)}"
            f"&filters[keywords]={quote(skill_name)}"
        )
        try:
            resp = await self._request_json(
                "GET", path,
                body=None,
                referer=f"{BASE}/resume/create/",
                page_type="resume-create",
            )
        except VacancyResponseError as exc:
            logger.warning(
                "[superjob] поиск навыка %r упал: %s", skill_name, exc,
            )
            return None
        q = _normalize(skill_name)
        for d in resp.get("data") or []:
            name = (d.get("attributes") or {}).get("name")
            if name and _normalize(name) == q:
                return d["id"]
        for d in resp.get("data") or []:
            return d["id"]
        return None

    async def resolve_institute(self, name: str) -> str | None:
        path = (
            "/institute/?page[limit]=15&page[offset]=0"
            f"&filters[keywords]={quote(name)}"
        )
        try:
            resp = await self._request_json(
                "GET", path,
                body=None,
                referer=f"{BASE}/resume/create/",
                page_type="resume-create",
            )
        except VacancyResponseError as exc:
            logger.warning("[superjob] поиск вуза %r упал: %s", name, exc)
            return None
        q = _normalize(name)
        for d in resp.get("data") or []:
            attrs = d.get("attributes") or {}
            if _normalize(attrs.get("abbreviation", "")) == q:
                return d["id"]
        for d in resp.get("data") or []:
            attrs = d.get("attributes") or {}
            if q in _normalize(attrs.get("name", "")):
                return d["id"]
        for d in resp.get("data") or []:
            return d["id"]
        return None

    async def resolve_all(self, resume: ResumeInput) -> ResolvedProfile:
        town_id = await self.resolve_town(resume.town)
        work_type_id = await self._resolve_dict(
            "workTypeDictionary", resume.work_type,
        )
        currency_id = await self._resolve_dict(
            "currencyDictionary", resume.currency,
        )
        citizenship_id = (
            await self._resolve_dict(
                "citizenshipDictionary", resume.citizenship,
            )
            if resume.citizenship
            else None
        )

        edu_levels: dict[str, str] = {}
        for e in resume.education:
            sid = await self._resolve_dict(
                "educationLevelDictionary", e.level, required=False,
            )
            if sid:
                edu_levels[e.level] = sid
        edu_forms: dict[str, str] = {}
        for e in resume.education:
            if not e.form:
                continue
            sid = await self._resolve_dict(
                "educationFormDictionary", e.form, required=False,
            )
            if sid:
                edu_forms[e.form] = sid
        lang_levels: dict[str, str] = {}
        languages: dict[str, str] = {}
        for lang in resume.languages:
            sid = await self._resolve_dict(
                "languageLevelDictionary", lang.level, required=False,
            )
            if sid:
                lang_levels[lang.level] = sid
            sid = await self._resolve_dict(
                "languageDictionary", lang.name, required=False,
            )
            if sid:
                languages[lang.name] = sid
        dls: dict[str, str] = {}
        for cat in resume.driving_licenses:
            sid = await self._resolve_dict(
                "drivingLicenseDictionary", cat, required=False,
            )
            if sid:
                dls[cat] = sid

        if not work_type_id or not currency_id:
            raise SuperJobAuthError(
                "не получилось зарезолвить базовые справочники "
                f"(work_type={work_type_id}, currency={currency_id})"
            )

        rp = ResolvedProfile(
            town_id=town_id,
            work_type_id=work_type_id,
            currency_id=currency_id,
            citizenship_id=citizenship_id,
            education_levels=edu_levels,
            education_forms=edu_forms,
            language_levels=lang_levels,
            languages=languages,
            driving_licenses=dls,
        )
        self.resolved = rp
        return rp

    # ── Регистрация ─────────────────────────────────────────────────
    async def check_contact(self, email: str) -> None:
        params_id = str(uuid.uuid4())
        contact_id = str(uuid.uuid4())
        body = {
            "data": {
                "id": str(uuid.uuid4()),
                "type": "checkContact",
                "attributes": {},
                "relationships": {
                    "params": {"data": {"id": params_id, "type": "checkContactParams"}}
                },
            },
            "included": [
                {
                    "id": params_id,
                    "type": "checkContactParams",
                    "attributes": {"silentValidate": False},
                    "relationships": {
                        "contacts": {"data": [{"id": contact_id, "type": "contact"}]}
                    },
                },
                {
                    "id": contact_id,
                    "type": "contact",
                    "attributes": {"value": email, "contactType": "email"},
                },
            ],
        }
        await self._request_json(
            "POST",
            "/action/checkContact/?include=params,params.contacts",
            body=body,
            referer=(
                f"{BASE}/?curtain%5BreturnUrl%5D=%2F"
                "&curtain%5BrouteName%5D=authLogin&curtain%5Bid%5D=auth"
            ),
            page_type="main-page",
        )

    async def create_guest_resume(
        self,
        resume: ResumeInput,
        work_type_id: str,
        town_id: str,
    ) -> None:
        sid = self.session_id or _random_session_id()
        short_session = sid[:42]
        body = {
            "data": {
                "id": str(uuid.uuid4()),
                "type": "resumeGuest",
                "attributes": {
                    "source": "desktop",
                    "sessionId": short_session,
                    "scenario": "pureCreation",
                    "email": resume.email,
                    "phone": "",
                    "firstName": resume.first_name,
                    "lastName": resume.last_name,
                    "birthDate": resume.birth_date,
                    "town": town_id,
                    "relocatePossibility": resume.relocate_possibility,
                    "moveableTowns": resume.moveable_towns,
                    "position": resume.position,
                    "salary": str(resume.salary),
                    "remoteWork": resume.remote_work,
                    "isVolunteer": resume.is_volunteer,
                    "businessTrip": resume.business_trip,
                    "vacancyId": "",
                },
                "relationships": {
                    "workType": {"data": {"id": work_type_id, "type": "workTypeDictionary"}},
                },
            },
            "included": [
                {"id": work_type_id, "type": "workTypeDictionary", "attributes": {}},
            ],
        }
        await self._request_json(
            "POST",
            "/resumeGuest/?include=workType",
            body=body,
            referer=(
                f"{BASE}/resume/create/?returnUrl=%2F&initialLogin={resume.email}"
            ),
            page_type="resume-create",
        )

    async def register(self, resume: ResumeInput) -> None:
        body = {
            "data": {
                "id": str(uuid.uuid4()),
                "type": "nodejsaccount",
                "attributes": {
                    "userType": "applicant",
                    "login": resume.email,
                    "name": resume.first_name,
                    "password": "",
                    "generatePassword": True,
                    "contact": {"email": resume.email},
                },
            }
        }
        resp = await self._request_json(
            "POST",
            "/nodejsaccount/",
            body=body,
            referer=(
                f"{BASE}/resume/create/?returnUrl=%2F&initialLogin={resume.email}"
            ),
            page_type="resume-create",
        )
        for inc in resp.get("included", []) or []:
            if inc.get("type") == "applicant":
                self.applicant_id = inc.get("id")
                break
        logger.info(
            "[superjob] зарегистрирован applicant_id=%s",
            self.applicant_id,
        )

    # ── Резюме + патчи ──────────────────────────────────────────────
    async def save_resume(
        self,
        resume: ResumeInput,
        resolved: ResolvedProfile,
    ) -> None:
        person_id = str(uuid.uuid4())
        birth_id = str(uuid.uuid4())
        salary_id = str(uuid.uuid4())
        detail_id = str(uuid.uuid4())
        privacy_id = str(uuid.uuid4())
        contact_id = str(uuid.uuid4())

        body = {
            "data": {
                "id": str(uuid.uuid4()),
                "type": "resume",
                "attributes": {
                    "position": resume.position,
                    "remoteWork": resume.remote_work,
                    "relocatePossibility": resume.relocate_possibility,
                    "isVolunteer": resume.is_volunteer,
                    "businessTrip": resume.business_trip,
                },
                "relationships": {
                    "workType": {"data": {"id": resolved.work_type_id, "type": "workTypeDictionary"}},
                    "town": {"data": {"id": resolved.town_id, "type": "town"}},
                    "person": {"data": {"id": person_id, "type": "resumePerson"}},
                    "resumeBirthDate": {"data": {"id": birth_id, "type": "resumeBirthDate"}},
                    "salary": {"data": {"id": salary_id, "type": "resumeSalary"}},
                    "currency": {"data": {"id": resolved.currency_id, "type": "currencyDictionary"}},
                    "detail": {"data": {"id": detail_id, "type": "resumeDetailInfo"}},
                    "userPrivacyAgreement": {"data": {"id": privacy_id, "type": "userPrivacyAgreement"}},
                    "contacts": {"data": [{"id": contact_id, "type": "contact"}]},
                },
            },
            "included": [
                {"id": resolved.work_type_id, "type": "workTypeDictionary", "attributes": {}},
                {"id": resolved.town_id, "type": "town", "attributes": {}},
                {
                    "id": person_id,
                    "type": "resumePerson",
                    "attributes": {
                        "firstName": resume.first_name,
                        "lastName": resume.last_name,
                    },
                },
                {
                    "id": birth_id,
                    "type": "resumeBirthDate",
                    "attributes": {
                        "birthDate": resume.birth_date,
                        "hideBirthday": False,
                    },
                },
                {
                    "id": salary_id,
                    "type": "resumeSalary",
                    "attributes": {"value": resume.salary},
                },
                {"id": resolved.currency_id, "type": "currencyDictionary", "attributes": {}},
                {
                    "id": detail_id,
                    "type": "resumeDetailInfo",
                    "attributes": {"smsEnabled": False},
                },
                {
                    "id": privacy_id,
                    "type": "userPrivacyAgreement",
                    "attributes": {},
                    "relationships": {
                        "privacyAgreement": {
                            "data": {
                                "id": resume.privacy_agreement,
                                "type": "privacyAgreementDictionary",
                            }
                        }
                    },
                },
                {
                    "id": resume.privacy_agreement,
                    "type": "privacyAgreementDictionary",
                    "attributes": {},
                },
                {
                    "id": contact_id,
                    "type": "contact",
                    "attributes": {
                        "value": resume.email,
                        "contactType": "email",
                    },
                },
            ],
        }
        include = (
            "workType,town,person,resumeBirthDate,salary,currency,detail,"
            "userPrivacyAgreement.privacyAgreement,contacts,totalExperience"
        )
        resp = await self._request_json(
            "POST",
            f"/resume/?include={include}",
            body=body,
            referer=f"{BASE}/resume/create/",
            page_type="resume-create",
        )
        data = resp.get("data") or {}
        self.resume_id = data.get("id")
        if not self.resume_id:
            raise SuperJobAuthError(f"не удалось получить resume_id: {resp}")
        logger.info("[superjob] создано резюме resume_id=%s", self.resume_id)

    async def patch_experience(
        self,
        resume: ResumeInput,
        resolved: ResolvedProfile,
    ) -> None:
        if not resume.experience:
            return
        exp_refs: list[dict[str, str]] = []
        included: list[dict[str, Any]] = []
        for exp in resume.experience:
            exp_id = str(uuid.uuid4())
            company_id = str(uuid.uuid4())
            town_id = (
                await self.resolve_town(exp.town) if exp.town else resolved.town_id
            )
            attributes: dict[str, Any] = {
                "position": exp.position,
                "dateStart": exp.date_start,
                "responsibility": exp.responsibility,
                "dateEnd": exp.date_end if exp.date_end else None,
            }
            exp_refs.append({"id": exp_id, "type": "resumeExperience"})
            included.append(
                {
                    "id": exp_id,
                    "type": "resumeExperience",
                    "attributes": attributes,
                    "relationships": {
                        "town": {"data": {"id": town_id, "type": "town"}},
                        "resumeCompany": {
                            "data": {
                                "id": company_id,
                                "type": "resumeExperienceCompany",
                            }
                        },
                    },
                }
            )
            included.append(
                {
                    "id": company_id,
                    "type": "resumeExperienceCompany",
                    "attributes": {"title": exp.company or "company"},
                }
            )
            included.append({"id": town_id, "type": "town", "attributes": {}})

        body = {
            "data": {
                "id": self.resume_id,
                "type": "resume",
                "attributes": {},
                "relationships": {"experience": {"data": exp_refs}},
            },
            "included": included,
        }
        include = (
            "experience,experience.resumeCompany,experience.town,totalExperience"
        )
        await self._request_json(
            "PATCH",
            f"/resume/{self.resume_id}/?include={include}",
            body=body,
            referer=f"{BASE}/resume/create/?resumeId={self.resume_id}",
            page_type="resume-create",
        )

    async def patch_skills(self, resume: ResumeInput) -> None:
        if not resume.skills:
            return
        skill_refs: list[dict[str, str]] = []
        included: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw in resume.skills:
            sid = await self.resolve_skill(resume.position, raw)
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            link_id = str(uuid.uuid4())
            skill_refs.append(
                {"id": link_id, "type": "resumeProfessionalSkill"}
            )
            included.append(
                {
                    "id": link_id,
                    "type": "resumeProfessionalSkill",
                    "attributes": {},
                    "relationships": {
                        "skill": {
                            "data": {"id": sid, "type": "professionalSkill"}
                        }
                    },
                }
            )
            included.append(
                {
                    "id": sid,
                    "type": "professionalSkill",
                    "attributes": {"name": raw},
                }
            )
        if not skill_refs:
            return
        body = {
            "data": {
                "id": self.resume_id,
                "type": "resume",
                "attributes": {},
                "relationships": {
                    "professionalSkills": {"data": skill_refs}
                },
            },
            "included": included,
        }
        await self._request_json(
            "PATCH",
            f"/resume/{self.resume_id}/?include=professionalSkills.skill",
            body=body,
            referer=f"{BASE}/resume/create/?resumeId={self.resume_id}",
            page_type="resume-create",
        )

    async def patch_education(
        self,
        resume: ResumeInput,
        resolved: ResolvedProfile,
    ) -> None:
        if not resume.education:
            return
        edu_refs: list[dict[str, str]] = []
        included: list[dict[str, Any]] = []
        for edu in resume.education:
            edu_id = str(uuid.uuid4())
            detail_id = str(uuid.uuid4())
            unknown_id = str(uuid.uuid4())
            level_id = (
                resolved.education_levels.get(edu.level)
                or await self._resolve_dict(
                    "educationLevelDictionary", edu.level, required=False,
                )
            )
            if not level_id:
                continue
            form_id = (
                resolved.education_forms.get(edu.form)
                if edu.form
                else None
            )
            inst_id = await self.resolve_institute(edu.institute)
            detail_attrs: dict[str, Any] = {
                "yearEnd": edu.year_end,
                "faculty": edu.faculty,
                "specialty": edu.specialty,
            }
            detail_rels: dict[str, Any] = {
                "unknownInstitute": {
                    "data": {"id": unknown_id, "type": "unknownInstitute"}
                }
            }
            if form_id:
                detail_rels["form"] = {
                    "data": {"id": form_id, "type": "educationFormDictionary"}
                }
            if inst_id:
                detail_rels["institute"] = {
                    "data": {"id": inst_id, "type": "institute"}
                }
            edu_refs.append({"id": edu_id, "type": "resumeEducation"})
            included.append(
                {
                    "id": edu_id,
                    "type": "resumeEducation",
                    "attributes": {},
                    "relationships": {
                        "level": {
                            "data": {
                                "id": level_id,
                                "type": "educationLevelDictionary",
                            }
                        },
                        "detail": {
                            "data": {
                                "id": detail_id,
                                "type": "resumeEducationDetail",
                            }
                        },
                    },
                }
            )
            included.append(
                {
                    "id": level_id,
                    "type": "educationLevelDictionary",
                    "attributes": {},
                }
            )
            included.append(
                {
                    "id": detail_id,
                    "type": "resumeEducationDetail",
                    "attributes": detail_attrs,
                    "relationships": detail_rels,
                }
            )
            if form_id:
                included.append(
                    {
                        "id": form_id,
                        "type": "educationFormDictionary",
                        "attributes": {},
                    }
                )
            if inst_id:
                included.append(
                    {"id": inst_id, "type": "institute", "attributes": {}}
                )
            included.append(
                {
                    "id": unknown_id,
                    "type": "unknownInstitute",
                    "attributes": {
                        "name": None if inst_id else edu.institute,
                    },
                }
            )
        if not edu_refs:
            return
        body = {
            "data": {
                "id": self.resume_id,
                "type": "resume",
                "attributes": {},
                "relationships": {"education": {"data": edu_refs}},
            },
            "included": included,
        }
        include = (
            "education.level,education.detail.form,"
            "education.detail.institute,"
            "education.detail.unknownInstitute,totalExperience"
        )
        await self._request_json(
            "PATCH",
            f"/resume/{self.resume_id}/?include={include}",
            body=body,
            referer=f"{BASE}/resume/create/?resumeId={self.resume_id}",
            page_type="resume-create",
        )

    async def patch_languages(
        self,
        resume: ResumeInput,
        resolved: ResolvedProfile,
    ) -> None:
        if not resume.languages:
            return
        refs: list[dict[str, str]] = []
        included: list[dict[str, Any]] = []
        for lang in resume.languages:
            link_id = str(uuid.uuid4())
            lang_id = (
                resolved.languages.get(lang.name)
                or await self._resolve_dict(
                    "languageDictionary", lang.name, required=False,
                )
            )
            level_id = (
                resolved.language_levels.get(lang.level)
                or await self._resolve_dict(
                    "languageLevelDictionary", lang.level, required=False,
                )
            )
            if not lang_id or not level_id:
                continue
            refs.append({"id": link_id, "type": "resumeLanguage"})
            included.append(
                {
                    "id": link_id,
                    "type": "resumeLanguage",
                    "attributes": {},
                    "relationships": {
                        "language": {
                            "data": {
                                "id": lang_id,
                                "type": "languageDictionary",
                            }
                        },
                        "level": {
                            "data": {
                                "id": level_id,
                                "type": "languageLevelDictionary",
                            }
                        },
                    },
                }
            )
        if not refs:
            return
        body = {
            "data": {
                "id": self.resume_id,
                "type": "resume",
                "attributes": {},
                "relationships": {"languages": {"data": refs}},
            },
            "included": included,
        }
        try:
            await self._request_json(
                "PATCH",
                (
                    f"/resume/{self.resume_id}/"
                    "?include=languages.language,languages.level"
                ),
                body=body,
                referer=f"{BASE}/resume/create/?resumeId={self.resume_id}",
                page_type="resume-create",
            )
        except VacancyResponseError as exc:
            logger.warning(
                "[superjob] PATCH languages не прошёл (%s) — пропускаем.",
                exc,
            )

    # ── Вакансии / отклики ──────────────────────────────────────────
    async def collect_vacancy_ids(
        self,
        responses_url: str = DEFAULT_RESPONSES_URL,
    ) -> list[str]:
        html = await self._get_html(responses_url)
        ids = sorted(set(VACANCY_ID_RE.findall(html)))
        logger.info("[superjob] найдено %d вакансий: %s", len(ids), ids)
        return ids

    async def save_vacancies_show(self, vacancy_id: str) -> None:
        params_id = str(uuid.uuid4())
        body = {
            "data": {
                "id": str(uuid.uuid4()),
                "type": "saveVacanciesShow",
                "attributes": {},
                "relationships": {
                    "params": {
                        "data": {
                            "id": params_id,
                            "type": "saveVacanciesShowParams",
                        }
                    }
                },
            },
            "included": [
                {
                    "id": params_id,
                    "type": "saveVacanciesShowParams",
                    "attributes": {"vacancyIds": [vacancy_id]},
                }
            ],
        }
        try:
            await self._request_json(
                "POST",
                "/action/saveVacanciesShow/?include=params",
                body=body,
                referer=DEFAULT_RESPONSES_URL,
                page_type="applicant-responses-list",
            )
        except VacancyResponseError as exc:
            logger.warning(
                "[superjob] saveVacanciesShow %s: %s", vacancy_id, exc,
            )

    async def apply_to_vacancy(self, vacancy_id: str) -> dict[str, Any]:
        if not self.resume_id:
            raise SuperJobAuthError(
                "resume_id не задан — сначала зарегистрируйте резюме."
            )
        vacancy_response_id = str(uuid.uuid4())
        cv_type_id = str(uuid.uuid4())
        body = {
            "data": {
                "id": str(uuid.uuid4()),
                "type": "cvApplication",
                "attributes": {},
                "relationships": {
                    "vacancyResponse": {
                        "data": {
                            "id": vacancy_response_id,
                            "type": "vacancyResponse",
                        }
                    }
                },
            },
            "included": [
                {
                    "id": vacancy_response_id,
                    "type": "vacancyResponse",
                    "attributes": {"noWorkExperience": False},
                    "relationships": {
                        "resume": {
                            "data": {
                                "id": self.resume_id,
                                "type": "resume",
                            }
                        },
                        "vacancy": {
                            "data": {"id": vacancy_id, "type": "vacancy"}
                        },
                        "cvApplicationType": {
                            "data": {
                                "id": cv_type_id,
                                "type": "cvApplicationType",
                            }
                        },
                    },
                },
                {"id": vacancy_id, "type": "vacancy", "attributes": {}},
                {"id": self.resume_id, "type": "resume", "attributes": {}},
                {
                    "id": cv_type_id,
                    "type": "cvApplicationType",
                    "attributes": {},
                    "relationships": {
                        "responseType": {
                            "data": {
                                "id": "default",
                                "type": "cvApplicationTypeDictionary",
                            }
                        }
                    },
                },
                {
                    "id": "default",
                    "type": "cvApplicationTypeDictionary",
                    "attributes": {},
                },
            ],
        }
        include = (
            "vacancyResponse,"
            "vacancyResponse.vacancy.resumeInteractions.status,"
            "vacancyResponse.vacancy.resumeInteractions.resume,"
            "vacancyResponse.vacancy.resumeInteractions.vacancyResponse,"
            "vacancyResponse.vacancy.detailInfo,"
            "vacancyResponse.vacancy.contactInfo,"
            "vacancyResponse.chat.resume,"
            "vacancyResponse.chat.company,"
            "vacancyResponse.resume,"
            "vacancyResponse.cvApplicationType.responseType,"
            "vacancyResponse.responseSource.sourceName"
        )
        return await self._request_json(
            "POST",
            f"/cvApplication/?include={include}",
            body=body,
            referer=DEFAULT_RESPONSES_URL,
            page_type="applicant-responses-list",
        )

    # ── Сборка vacancy_responses-строки ─────────────────────────────
    @staticmethod
    def _extract_vacancy_card(
        cv_response: dict[str, Any],
        vacancy_id: str,
    ) -> dict[str, Any]:
        """Из ответа ``cvApplication`` выдёргиваем поля для UI:
        title, company, salary, link, logo url. Если каких-то полей
        в HAR нет — оставляем пустыми и доверяем дашборду рисовать
        фоллбек."""
        title = ""
        company = ""
        salary = ""
        logo_url = ""
        for inc in cv_response.get("included", []) or []:
            if inc.get("type") == "vacancy" and inc.get("id") == vacancy_id:
                attrs = inc.get("attributes") or {}
                title = str(
                    attrs.get("profession") or attrs.get("title") or ""
                )
                salary = str(
                    attrs.get("paymentTitle") or attrs.get("paymentFrom") or ""
                )
            if inc.get("type") in ("client", "company"):
                attrs = inc.get("attributes") or {}
                company = company or str(attrs.get("title") or "")
                logo_url = logo_url or str(attrs.get("logoUrl") or "")
        return {
            "title": title,
            "company": company,
            "salary": salary,
            "logo_url": logo_url,
            "link": f"{BASE}/vakansii/-{vacancy_id}.html",
        }

    # ── Подтверждение email + смена пароля ──────────────────────────
    async def confirm_email_via_link(
        self,
        *,
        email: str,
        token: str | None,
    ) -> bool:
        """Опросить inbox юзера, найти confirmation-ссылку и пройти по ней.

        После ``POST /nodejsaccount/`` SuperJob шлёт письмо вида
        ``Подтверждение почтового адреса пользователя SuperJob`` с
        отправителя ``nobody@host.superjob.ru``. В письме — одна
        ссылка на ``superjob.ru``, по которой нужно перейти один раз,
        чтобы аккаунт получил ``confirmationStatus.email = true`` и
        дальнейший PATCH пароля сработал.

        Без ``temp_email_token`` (юзер пришёл с реальным email-ом и у
        нас нет доступа к его inbox-у) метод штатно возвращает
        ``False`` — флоу не падает, просто пропускаем подтверждение.
        Тогда SuperJob оставит ``confirmationStatus.email = false`` и
        смена пароля по той же сессии всё равно проходит (HAR
        показывает, что PATCH ``/nodejsauth/0/`` принимается у юзера
        в любом статусе, пока есть кука сессии после регистрации).
        """
        if not token:
            logger.info(
                "[superjob] у user=%s нет temp_email_token — "
                "пропускаем confirmation-link",
                self.user_id,
            )
            return False
        logger.info(
            "[superjob] ждём confirmation-ссылку на %s …", email,
        )
        link = await wait_for_link(
            email,
            token,
            sender_contains="superjob",
            subject_contains="superjob",
            link_contains="superjob.ru",
            timeout=120.0,
        )
        if not link:
            logger.warning(
                "[superjob] confirmation-ссылка не пришла на %s за 120с",
                email,
            )
            return False

        logger.info("[superjob] переходим по confirmation-ссылке %s…", link[:80])
        try:
            async with self.session.get(
                link,
                headers={
                    "User-Agent": DEFAULT_UA,
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,*/*;q=0.8"
                    ),
                    "Referer": BASE,
                },
                allow_redirects=True,
            ) as r:
                # 200 — ок. 302/200 после редиректов — ок. Любая 4xx —
                # подтверждение уже было пройдено / ссылка протухла;
                # это не повод падать на ровном месте.
                _ = await r.read()
                logger.info(
                    "[superjob] confirmation-ссылка пройдена, status=%s",
                    r.status,
                )
                return r.status < 400
        except aiohttp.ClientError as exc:
            logger.warning(
                "[superjob] confirmation-ссылка не открылась: %s", exc,
            )
            return False

    async def _fetch_auth_id(self) -> str | None:
        """Достать ``authId`` текущей сессии (нужен для PATCH пароля).

        После регистрации в ответе ``POST /nodejsaccount/`` приходит
        блок ``data.attributes.authId`` — но мы его не сохраняли (раньше
        был не нужен). Берём свежее значение через GET той же ручки,
        что SuperJob дёргает на странице ``/user/``.
        """
        try:
            resp = await self._request_json(
                "GET",
                "/nodejsauth/0/?include=resetOperation",
                referer=f"{BASE}/user/",
                page_type="applicant-profile",
            )
        except (VacancyResponseError, aiohttp.ClientError) as exc:
            logger.warning(
                "[superjob] GET /nodejsauth/0/ не удался: %s", exc,
            )
            return None
        data = resp.get("data") or {}
        attrs = data.get("attributes") or {}
        auth_id = attrs.get("authId")
        return str(auth_id) if auth_id else None

    async def change_password(self, new_password: str) -> bool:
        """Сменить пароль на детерминированный ``new_password``.

        Формат запроса воспроизведён из HAR'а (см. user-attached
        ``+.har``, entry #12)::

            PATCH /jsapi3/0.1/nodejsauth/0/?include=resetOperation
            {
              "data": {
                "id": "0", "type": "nodejsauth",
                "attributes": {"authId": "<applicant_authId>"},
                "relationships": {"resetOperation": {"data": {
                  "id": "<uuid4>", "type": "passwordResetOperation"
                }}}
              },
              "included": [{
                "id": "<same uuid4>", "type": "passwordResetOperation",
                "attributes": {
                  "destination": "email",
                  "password": "<новый пароль>"
                }
              }]
            }

        Используем ровно тот пароль, что юзер видит в ``/settings``
        («Доступы для ручного входа») — :func:`generate_platform_password`.
        """
        if not self.user_id:
            logger.warning(
                "[superjob] change_password: user_id не задан, пропускаем",
            )
            return False
        auth_id = await self._fetch_auth_id()
        if not auth_id:
            logger.warning(
                "[superjob] change_password: не получили authId — "
                "пароль не сменили",
            )
            return False

        op_id = str(uuid.uuid4())
        body = {
            "data": {
                "id": "0",
                "type": "nodejsauth",
                "attributes": {"authId": auth_id},
                "relationships": {
                    "resetOperation": {
                        "data": {
                            "id": op_id,
                            "type": "passwordResetOperation",
                        },
                    },
                },
            },
            "included": [
                {
                    "id": op_id,
                    "type": "passwordResetOperation",
                    "attributes": {
                        "destination": "email",
                        "password": new_password,
                    },
                },
            ],
        }
        try:
            await self._request_json(
                "PATCH",
                "/nodejsauth/0/?include=resetOperation",
                body=body,
                referer=(
                    f"{BASE}/user/?profileSettings%5BprofileDataFormId"
                    f"%5D=APPLICANT_PROFILE_AUTH_PASSWORD_FORM"
                ),
                page_type="applicant-profile",
            )
        except (VacancyResponseError, aiohttp.ClientError) as exc:
            logger.warning(
                "[superjob] PATCH /nodejsauth/0/ не удался: %s", exc,
            )
            return False
        logger.info(
            "[superjob] пароль на SuperJob сменён "
            "(authId=%s, len=%d)", auth_id, len(new_password),
        )
        return True

    # ── Высокоуровневая операция воркера ────────────────────────────
    async def ensure_registered(
        self,
        resume: ResumeInput,
        *,
        temp_email_token: str | None = None,
    ) -> None:
        """Если ``self.resume_id`` уже есть (поднят из БД) — выходим.
        Иначе разово регистрируемся, подтверждаем email, ставим
        детерминированный пароль и создаём резюме."""
        if self.resume_id:
            return
        await self.warmup()
        resolved = await self.resolve_all(resume)
        await self.check_contact(resume.email)
        await self.create_guest_resume(
            resume, resolved.work_type_id, resolved.town_id,
        )
        await self.register(resume)
        # После регистрации SJ шлёт письмо с confirmation-ссылкой.
        # Идём по ней, чтобы поднять confirmationStatus.email=true.
        await self.confirm_email_via_link(
            email=resume.email,
            token=temp_email_token,
        )
        # Ставим тот же пароль, что юзер видит в /settings.
        if self.user_id:
            new_password = generate_platform_password(self.user_id)
            await self.change_password(new_password)
        await self.save_resume(resume, resolved)
        await self.patch_experience(resume, resolved)
        await self.patch_skills(resume)
        await self.patch_education(resume, resolved)
        if resume.languages:
            await self.patch_languages(resume, resolved)
        # Сразу после регистрации сбрасываем cookies в БД, чтобы при
        # любой ошибке в дальнейшем мы не теряли только что выданный
        # ``resume_id``.
        await self._persist_session()

    async def respond_to_vacancies(self, ctx) -> dict[str, Any]:
        """Откликнуться на все «подходящие вакансии» на SuperJob.

        Объединяет за один проход всё, что нужно сделать воркеру для
        одной итерации SJ-площадки:

            1. Тянет резюме юзера из БД и конвертирует в
               :class:`ResumeInput`. Если данных не хватает —
               отдаёт ``error=missing_resume_data``.
            2. Если в БД нет ``resume_id``, выполняет «холодный»
               сценарий: регистрация + создание резюме + патчи опыта/
               навыков/образования/языков.
            3. Парсит вакансии со страницы ``/user/responses/`` и
               отсылает отклик на каждую с паузой между запросами.
            4. На каждый отклик пишет строку в ``vacancy_responses``
               (та же логика, что у habr — ``service='superjob'``,
               ``is_sent`` / ``error``).

        Возвращает совместимую с habr/hh структуру: ``scraped_vacancies``,
        ``cover_letters_generated`` (всегда 0 — SJ не принимает
        cover letter в этом флоу), ``responses``.
        """
        scraped_vacancies = 0
        cover_letters_generated = 0
        responses: list[dict] = []

        bootstrap = await _load_superjob_resume_input(ctx.user_id)
        if bootstrap is None:
            logger.warning(
                "[superjob] недостаточно данных в резюме — "
                "отклики невозможны (user=%s)", ctx.user_id,
            )
            return {
                "scraped_vacancies": scraped_vacancies,
                "cover_letters_generated": cover_letters_generated,
                "responses": responses,
                "error": "missing_resume_data",
            }
        resume_input = bootstrap.resume

        # Email из резюме — стабильный идентификатор для in-memory
        # bridge'а и логина. Подменяем тот, что прислал from_context.
        self.email = resume_input.email

        try:
            await self.ensure_registered(
                resume_input,
                temp_email_token=bootstrap.temp_email_token,
            )
        except SuperJobAuthError as exc:
            logger.warning(
                "[superjob] регистрация/логин не удались (user=%s): %s",
                ctx.user_id, exc,
            )
            return {
                "scraped_vacancies": scraped_vacancies,
                "cover_letters_generated": cover_letters_generated,
                "responses": responses,
                "error": "session_expired",
            }

        try:
            vacancy_ids = await self.collect_vacancy_ids()
        except (VacancyResponseError, aiohttp.ClientError) as exc:
            logger.warning(
                "[superjob] не удалось получить список вакансий: %s", exc,
            )
            return {
                "scraped_vacancies": scraped_vacancies,
                "cover_letters_generated": cover_letters_generated,
                "responses": responses,
                "error": "list_failed",
            }

        if not vacancy_ids:
            logger.info("[superjob] подходящих вакансий не нашлось")
            return {
                "scraped_vacancies": scraped_vacancies,
                "cover_letters_generated": cover_letters_generated,
                "responses": responses,
            }

        for vacancy_id in vacancy_ids:
            scraped_vacancies += 1
            try:
                await self.save_vacancies_show(vacancy_id)
                resp = await self.apply_to_vacancy(vacancy_id)
                result: dict[str, Any] = {
                    "vacancy_id": vacancy_id,
                    "ok": True,
                    "cv_application_id": (
                        (resp.get("data") or {}).get("id")
                    ),
                }
            except AlreadyAppliedError:
                result = {"already": True, "vacancy_id": vacancy_id}
                resp = {}
            except RateLimitedError as exc:
                logger.info(
                    "[superjob] rate-limit на #%s, жду 12с", vacancy_id,
                )
                await asyncio.sleep(12.0)
                result = {
                    "vacancy_id": vacancy_id,
                    "error": "rate_limited",
                    "status": exc.status,
                }
                resp = {}
            except VacancyResponseError as exc:
                result = {
                    "vacancy_id": vacancy_id,
                    "error": str(exc),
                    "status": exc.status,
                }
                resp = {}

            responses.append(result)
            error = result.get("error")
            card = self._extract_vacancy_card(resp, vacancy_id) if resp else {
                "title": resume_input.position,
                "company": "",
                "salary": "",
                "logo_url": "",
                "link": f"{BASE}/vakansii/-{vacancy_id}.html",
            }

            vacancy_response_id = uuid.uuid4()
            logo_ext: str | None = None
            if card.get("logo_url"):
                try:
                    logo_ext = await download_company_logo(
                        card["logo_url"],
                        vacancy_response_id,
                        session=self.session,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "[superjob] не скачался логотип %s: %s",
                        card.get("logo_url"), exc,
                    )

            async with session_scope() as db:
                vr = VacancyResponse(
                    id=vacancy_response_id,
                    user_id=ctx.user_id,
                    title=(card.get("title") or resume_input.position)[:255],
                    company=(card.get("company") or None),
                    salary=(card.get("salary") or None),
                    service=PLATFORM_SUPERJOB,
                    link=(card.get("link") or "")[:128],
                    logo_ext=logo_ext,
                    is_sent=bool(not error),
                    error=error,
                )
                db.add(vr)
                await db.flush()
                await notify_response_change(db, ctx.user_id)
                user_row = (
                    await db.execute(
                        select(User).where(User.id == ctx.user_id)
                    )
                ).scalar_one_or_none()
                if user_row is None or not user_row.auto_apply_running:
                    break

            await asyncio.sleep(random.uniform(*VACANCY_RESPONSE_DELAY))

        return {
            "scraped_vacancies": scraped_vacancies,
            "cover_letters_generated": cover_letters_generated,
            "responses": responses,
        }


__all__ = [
    "SuperJobClient",
    "ResumeInput",
    "ResumeExperience",
    "ResumeEducation",
    "ResumeLanguageInput",
    "ResolvedProfile",
    "AlreadyAppliedError",
    "VacancyResponseError",
    "RateLimitedError",
    "SuperJobAuthError",
]
