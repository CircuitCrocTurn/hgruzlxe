"""
habr.com / career.habr.com client — асинхронный, переиспользует сессию.

Зависимости::

    pip install aiohttp beautifulsoup4 2captcha-python

Покрытые сценарии:

1. Регистрация или логин на ``account.habr.com``.
2. SSO на ``career.habr.com``.
3. Прохождение онбординга Хабр Карьеры (все категории + ``/onboarding/complete``).
4. Получение списка подходящих вакансий и автоматическая рассылка откликов.

Сессия (де)сериализуется в ``platform_credentials.encrypted_session``
через унаследованные ``save_session``/``load_session`` из
:class:`BaseJobSiteClient` — на диск (как раньше) больше ничего не пишем.

См. :mod:`app.services.job_sites.hh` — реализация habr-клиента
зеркалит hh-клиент по структуре (dataclass + ``async with``
+ OTP-бридж + ``from_context``).
"""

from __future__ import annotations

import asyncio
import logging
import pickle
import random
import re
import secrets
import string
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional, Tuple

import aiohttp
from aiohttp import ClientSession, CookieJar
from bs4 import BeautifulSoup
from twocaptcha import TwoCaptcha

from app.config import TWOCAPTCHA_KEY
from app.services.ai.cover_letter import generate_cover_letter
from app.services.job_sites.base import BaseJobSiteClient
from app.services.job_sites.habr.convert_page_to_post_data import build_post_body
from app.services.job_sites.habr.vacancy_extractor import extract_vacancy
from app.services.job_sites.registry import PLATFORM_HABR


# ---------------------------------------------------------------------------
# Константы
# ---------------------------------------------------------------------------

ACCOUNT_BASE = "https://account.habr.com"
BASE = "https://career.habr.com"

CAREER_SSO_URL = f"{BASE}/users/auth/tmid"
CAREER_ONBOARDING_URL = f"{BASE}/onboarding"
CAREER_ONBOARDING_COMPLETE_URL = f"{CAREER_ONBOARDING_URL}/complete"
CAREER_VACANCIES_URL = f"{BASE}/vacancies"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
)

PROFILE_CATEGORIES: Tuple[str, ...] = (
    "specialization",
    "salary",
    "experience",
    "profile",
)

RESUME_POLL_INTERVAL = 5.0            # сек между опросами /hh_resume_progress
VACANCY_RESPONSE_DELAY = (11.0, 14.0) # пауза между откликами (с). Career
                                      # ограничивает 1 отклик в 10 секунд,
                                      # поэтому 11–14с — безопасный минимум.
VACANCY_RATE_LIMIT_BACKOFF = 12.0     # сколько ждать, если прилетел rate-limit
VACANCY_MAX_PAGES = 20                # страховка от бесконечной пагинации


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

logger = logging.getLogger("habr")


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)-5s] %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# In-memory OTP-бридж (см. документацию в app/services/job_sites/hh)
# ---------------------------------------------------------------------------
_OTP_BRIDGE: dict[str, bytes] = {}


async def migrate_otp_bridge_to_db(habr_email: str, user_id: uuid.UUID) -> bool:
    """Перенести OTP-сессию из in-memory бриджа в ``platform_credentials``."""
    blob = _OTP_BRIDGE.pop(habr_email, None)
    if blob is None:
        return False
    placeholder = HabrClient(email=habr_email, user_id=user_id)
    await placeholder.save_session(user_id=user_id, blob=blob)
    return True


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

def generate_password(length: int = 10) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choice(alphabet) for _ in range(length))


async def prompt(question: str) -> str:
    """Неблокирующая обёртка над ``input`` для asyncio."""
    return await asyncio.to_thread(input, question)


def _multipart_boundary() -> str:
    return "----geckoformboundary" + secrets.token_hex(16)


def _build_empty_multipart() -> Tuple[str, bytes]:
    """Пустое multipart-тело (только закрывающий boundary)."""
    boundary = _multipart_boundary()
    body = f"--{boundary}--\r\n".encode()
    return boundary, body


def _build_multipart_fields(fields: Iterable[Tuple[str, str]]) -> Tuple[str, bytes]:
    """Собирает multipart-тело из пар ``(name, value)``."""
    boundary = _multipart_boundary()
    chunks: List[bytes] = []
    for name, value in fields:
        chunk = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        )
        chunks.append(chunk.encode("utf-8"))
    chunks.append(f"--{boundary}--\r\n".encode())
    return boundary, b"".join(chunks)


# ---------------------------------------------------------------------------
# 2Captcha (Yandex Smart)
# ---------------------------------------------------------------------------


@dataclass
class CaptchaResult:
    success: bool
    token: Optional[str] = None
    error: Optional[str] = None


class YandexCaptchaSolver:
    """Тонкая обёртка над синхронным клиентом 2Captcha."""

    def __init__(self, api_token: Optional[str] = None) -> None:
        token = api_token or TWOCAPTCHA_KEY
        if not token:
            raise RuntimeError("TWOCAPTCHA_KEY не задан")
        self._solver = TwoCaptcha(token, defaultTimeout=180, pollingInterval=5)

    async def solve(self, url: str, sitekey: str) -> CaptchaResult:
        def _solve() -> CaptchaResult:
            try:
                balance = self._solver.balance()
                logger.info("captcha: sitekey=%s balance=%s", sitekey, balance)
                result = self._solver.yandex_smart(url=url, sitekey=sitekey)
                return CaptchaResult(success=True, token=result["code"])
            except Exception as exc:  # noqa: BLE001
                return CaptchaResult(success=False, error=str(exc))

        return await asyncio.to_thread(_solve)


# ---------------------------------------------------------------------------
# DTO / исключения
# ---------------------------------------------------------------------------


class AlreadyAppliedError(RuntimeError):
    """career.habr.com ответил 401 на POST /responses — отклик уже был."""


class VacancyResponseError(RuntimeError):
    """HTTP-ошибка при отклике (400/403/422/др.)."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


class RateLimitedError(VacancyResponseError):
    """Career вернул 400 с сообщением о частоте откликов (≈ 1 раз в 10с)."""


def _is_rate_limited(body: str) -> bool:
    return "чаще, чем раз" in body or "раз в 10 секунд" in body


# ---------------------------------------------------------------------------
# Клиент Habr
# ---------------------------------------------------------------------------


@dataclass
class HabrClient(BaseJobSiteClient):
    """Единая сессия для ``account.habr.com`` и ``career.habr.com``.

    Регистрируется автоматически благодаря наследованию от
    :class:`BaseJobSiteClient` и классовому атрибуту ``slug``.
    Воркер достаёт класс через ``get_client_class("habr")``.

    Поля:
        email: e-mail юзера на habr (для OTP-бриджа и логина).
        user_id: UUID юзера в нашей БД. Если задан — cookies
            (де)сериализуются из/в ``platform_credentials
            .encrypted_session``. Если ``None`` — cookies живут
            в in-memory ``_OTP_BRIDGE`` (краткое окно регистрации,
            до момента, когда юзер появится в БД).
    """

    slug = PLATFORM_HABR

    email: str
    user_id: uuid.UUID | None = None

    # внутреннее состояние
    session: ClientSession = field(init=False, repr=False)
    _solver: YandexCaptchaSolver = field(init=False, repr=False)

    # ── lifecycle ─────────────────────────────────────────────────────

    async def __aenter__(self) -> "HabrClient":
        jar = CookieJar(quote_cookie=False)
        await self._restore_cookies_into(jar)

        self.session = ClientSession(
            cookie_jar=jar,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        )
        self._solver = YandexCaptchaSolver()
        return self

    async def __aexit__(self, *exc):
        try:
            if any(True for _ in self.session.cookie_jar):  # noqa
                await self._persist_cookies()
        except Exception as e:
            logger.warning("не удалось сохранить cookies: %s", e)
        await self.session.close()

    # ── сохранение / восстановление сессии ────────────────────────────

    def _cookies_to_blob(self) -> bytes:
        return pickle.dumps(list(self.session.cookie_jar))

    @staticmethod
    def _blob_to_cookies(blob: bytes) -> list:
        return pickle.loads(blob)

    async def _restore_cookies_into(self, jar: CookieJar) -> None:
        """Заливает cookies в свежий ``CookieJar`` из БД или бриджа."""
        if self.user_id is not None:
            try:
                blob = await self.load_session(user_id=self.user_id)
            except Exception as e:
                logger.warning("[session] load_session из БД упал: %s", e)
                blob = None
            if blob:
                try:
                    for cookie in self._blob_to_cookies(blob):
                        jar.update_cookies({cookie.key: cookie})
                    logger.info(
                        "[session] cookies загружены из БД (user_id=%s)",
                        self.user_id,
                    )
                    return
                except Exception as e:
                    logger.warning(
                        "[session] не удалось разобрать blob из БД: %s", e
                    )

        bridge_blob = _OTP_BRIDGE.get(self.email)
        if bridge_blob:
            try:
                for cookie in self._blob_to_cookies(bridge_blob):
                    jar.update_cookies({cookie.key: cookie})
                logger.info("[session] cookies загружены из in-memory бриджа")
            except Exception as e:
                logger.warning(
                    "[session] не удалось разобрать blob из бриджа: %s", e
                )

    async def _persist_cookies(self) -> None:
        """Сохраняет cookies в БД (если есть ``user_id``) или в бридж."""
        blob = self._cookies_to_blob()
        if self.user_id is not None:
            await self.save_session(user_id=self.user_id, blob=blob)
            _OTP_BRIDGE.pop(self.email, None)
            logger.debug(
                "[session] cookies сохранены в БД (user_id=%s)", self.user_id
            )
        else:
            _OTP_BRIDGE[self.email] = blob
            logger.debug(
                "[session] cookies сохранены в in-memory бридж (email=%s)",
                self.email,
            )

    @classmethod
    def from_context(cls, ctx):
        """Фабрика для воркера.

        ``WorkerContext`` сейчас несёт только ``phone_number``; до тех пор,
        пока в нём не появится ``habr_email`` (или эквивалент в ``prefs``),
        используем ``phone_number`` как идентификатор — в БД важна не
        строка-ключ сама по себе, а связка с ``user_id``.
        """
        email = (
            getattr(ctx, "habr_email", None)
            or getattr(getattr(ctx, "prefs", None), "habr_email", None)
            or ctx.phone_number
        )
        if not email:
            raise ValueError(
                "в WorkerContext нет email/phone_number для habr"
            )
        return cls(email=email, user_id=ctx.user_id)

    # ── account.habr.com ──────────────────────────────────────────────

    async def discover_register_flow(self) -> Tuple[str, str, str]:
        """Возвращает ``(register_link, page_token, yandex_sitekey)``."""
        logger.info("[account] открываю %s", ACCOUNT_BASE)
        async with self.session.get(f"{ACCOUNT_BASE}/") as r:
            r.raise_for_status()
            html = await r.text()
        soup = BeautifulSoup(html, "html.parser")

        links = soup.find_all("a", class_="form-additional-message__link")
        if not links:
            raise RuntimeError(
                "На account.habr.com не найдена ссылка на регистрацию"
            )
        register_link = str(links[0].get("href"))
        page_token = register_link.rstrip("/").split("/")[-1]
        logger.info(
            "[account] register_link=%s page_token=%s",
            register_link, page_token,
        )

        async with self.session.get(register_link) as register_page:
            register_page.raise_for_status()
            register_html = await register_page.text()
        soup = BeautifulSoup(register_html, "html.parser")

        captcha_div = soup.find("div", attrs={"data-captcha": "yandex"})
        if not captcha_div or not captcha_div.get("data-sitekey"):
            raise RuntimeError("На странице регистрации не найден data-sitekey")
        sitekey = str(captcha_div["data-sitekey"])
        logger.info("[account] yandex sitekey=%s", sitekey)

        return register_link, page_token, sitekey

    async def start_register(
        self,
        *,
        register_link: str,
        page_token: str,
        sitekey: str,
        email: str,
        password: str,
    ) -> dict[str, Any]:
        logger.info("[account] решаю капчу для регистрации")
        captcha = await self._solver.solve(register_link, sitekey)
        if not captcha.success:
            raise RuntimeError(f"Капча не решена: {captcha.error}")

        payload = {
            "email": email,
            "nickname": email.split("@")[0],
            "password1": password,
            "password2": password,
            "agree": "on",
            "cplcy": "on",
            "smart-token": captcha.token,
        }
        logger.info("[account] POST /ru/register/start (email=%s)", email)
        async with self.session.post(
            f"{ACCOUNT_BASE}/ru/register/start/{page_token}",
            data=payload,
        ) as r:
            data = await r.json(content_type=None)
        logger.info("[account] register/start -> %s", data)
        return data

    async def finish_register(
        self, page_token: str, code: str
    ) -> dict[str, Any]:
        logger.info("[account] POST /ru/register/finish")
        async with self.session.post(
            f"{ACCOUNT_BASE}/ru/register/finish/{page_token}",
            data={"code": code},
        ) as r:
            data = await r.json(content_type=None)
        logger.info("[account] register/finish -> %s", data)
        return data

    async def login(
        self,
        *,
        page_token: str,
        sitekey: str,
        email: str,
        password: str,
    ) -> bool:
        login_link = f"{ACCOUNT_BASE}/ru/ident/in/{page_token}"
        logger.info("[account] решаю капчу для логина")
        captcha = await self._solver.solve(login_link, sitekey)
        if not captcha.success:
            raise RuntimeError(
                f"Капча на логине не решена: {captcha.error}"
            )

        logger.info("[account] POST %s (email=%s)", login_link, email)
        async with self.session.post(
            login_link,
            data={
                "email": email,
                "password": password,
                "smart-token": captcha.token,
            },
        ) as r:
            status = r.status
            try:
                data = await r.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                logger.error(
                    "[account] логин вернул не-JSON (status=%s)", status
                )
                return False
        ok = bool(data.get("success"))
        logger.info("[account] login -> success=%s payload=%s", ok, data)
        return ok

    # ── career.habr.com ───────────────────────────────────────────────

    async def sso_to_career(self) -> str:
        """Переносит логин account.habr.com в career.habr.com через OAuth."""
        logger.info("[career] SSO через %s", CAREER_SSO_URL)
        async with self.session.get(
            CAREER_SSO_URL,
            headers={"Referer": f"{ACCOUNT_BASE}/"},
        ) as r:
            await r.read()
            final = str(r.url)
        if "career.habr.com" not in final or "/users/auth/" in final:
            raise RuntimeError(f"SSO не прошёл (последний URL: {final})")
        logger.info("[career] SSO ок, финальный URL=%s", final)
        return final

    # ── состояние сессии ──────────────────────────────────────────────

    async def is_authenticated(self) -> bool:
        """Проверяет, валидна ли сессия на career.habr.com."""
        try:
            async with self.session.get(f"{BASE}/") as r:
                r.raise_for_status()
                final = str(r.url)
                await r.read()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[career] проверка сессии упала: %s", exc)
            return False
        authed = "/users/auth/" not in final
        logger.info("[career] сессия валидна: %s (url=%s)", authed, final)
        return authed

    async def needs_onboarding(self) -> bool:
        """True, если онбординг ещё не пройден."""
        async with self.session.get(CAREER_ONBOARDING_URL) as r:
            r.raise_for_status()
            await r.read()
            final = str(r.url)
        needs = "/onboarding" in final
        logger.info(
            "[career] нужен ли онбординг: %s (final=%s)", needs, final
        )
        return needs

    async def _career_page(self, url: str) -> Tuple[str, str]:
        """GET страницы career.habr.com. Возвращает ``(csrf_token, html)``."""
        async with self.session.get(url) as r:
            r.raise_for_status()
            final = str(r.url)
            text = await r.text()
        if "/users/auth/" in final:
            raise RuntimeError(
                f"Не залогинен на career.habr.com (редирект на {final})"
            )
        soup = BeautifulSoup(text, "html.parser")
        meta = soup.find("meta", attrs={"name": "csrf-token"})
        if meta is None:
            raise RuntimeError(f"csrf-token meta не найден на {url}")
        csrf = str(meta.get("content", ""))
        logger.debug("[career] csrf для %s = %s…", url, csrf[:8])
        return csrf, text

    @staticmethod
    def _ajax_headers(csrf_token: str, referer: str) -> dict[str, str]:
        return {
            "X-CSRF-Token": csrf_token,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": referer,
            "Origin": BASE,
        }

    # ── онбординг: подтягивание резюме + категории ────────────────────

    async def fetch_hh_resume(self, resume_link: str) -> dict[str, Any]:
        csrf, _ = await self._career_page(CAREER_ONBOARDING_URL)
        logger.info("[career] fetch_hh_resume link=%s", resume_link)
        async with self.session.post(
            f"{CAREER_ONBOARDING_URL}/fetch_hh_resume",
            data={"link": resume_link},
            headers=self._ajax_headers(csrf, CAREER_ONBOARDING_URL),
        ) as r:
            r.raise_for_status()
            data = await r.json(content_type=None)
        logger.info("[career] fetch_hh_resume -> %s", data)
        return data

    async def wait_hh_resume_ready(
        self, poll_interval: float = RESUME_POLL_INTERVAL
    ) -> dict[str, Any]:
        logger.info(
            "[career] жду готовности hh-резюме, poll=%.1fs", poll_interval
        )
        while True:
            csrf, _ = await self._career_page(CAREER_ONBOARDING_URL)
            async with self.session.post(
                f"{CAREER_ONBOARDING_URL}/hh_resume_progress",
                headers=self._ajax_headers(csrf, CAREER_ONBOARDING_URL),
            ) as r:
                r.raise_for_status()
                data = await r.json(content_type=None)
            status = data.get("status")
            logger.info("[career] hh_resume_progress status=%s", status)
            if status != "init":
                return data
            await asyncio.sleep(poll_interval)

    async def submit_profile_category(self, category: str) -> dict[str, Any]:
        url = f"{BASE}/onboarding/{category}"
        logger.info("[career] заполняю категорию: %s", category)
        csrf, html = await self._career_page(url)
        form_data = build_post_body(html)
        logger.debug(
            "[career] form_data[%s] ключи=%s",
            category, list(form_data.keys()),
        )

        async with self.session.post(
            url,
            data=form_data,
            headers=self._ajax_headers(csrf, url),
        ) as r:
            r.raise_for_status()
            status = r.status
            try:
                data = await r.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                data = {
                    "status_code": status,
                    "text": (await r.text())[:200],
                }
        logger.info("[career] %s -> %s", category, data)
        return data

    async def complete_onboarding(self) -> dict[str, Any]:
        """Финальный шаг онбординга: POST /onboarding/complete с _method=put."""
        logger.info("[career] завершаю онбординг (complete)")
        csrf, _ = await self._career_page(CAREER_ONBOARDING_COMPLETE_URL)
        async with self.session.post(
            CAREER_ONBOARDING_COMPLETE_URL,
            data={
                "_method": "put",
                "authenticity_token": csrf,
            },
            headers={
                "Referer": CAREER_ONBOARDING_COMPLETE_URL,
                "Origin": BASE,
                # обычный form submit — НЕ XHR, поэтому без X-CSRF-Token/XHR
            },
        ) as r:
            r.raise_for_status()
            status = r.status
            final = str(r.url)
            try:
                data = await r.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                data = {"status_code": status, "url": final}
        logger.info("[career] /onboarding/complete -> %s", data)
        return data

    # ── вакансии и отклики ────────────────────────────────────────────

    async def parse_vacancies(
        self,
        *,
        vacancy_type: str = "suitable",
        sort: str = "date",
        max_pages: int = VACANCY_MAX_PAGES,
    ) -> List[str]:
        """Собирает id вакансий со всех страниц выдачи.

        ``vacancy_type`` — значение query-параметра ``type`` (например
        ``suitable``, ``all``, ``favorite``).
        """
        all_ids: List[str] = []
        seen: set[str] = set()

        for page in range(1, max_pages + 1):
            url = (
                f"{CAREER_VACANCIES_URL}?sort={sort}"
                f"&type={vacancy_type}&page={page}"
            )
            logger.info(
                "[career] список вакансий (%s): стр. %d -> %s",
                vacancy_type, page, url,
            )
            _, html = await self._career_page(url)
            page_ids = self.parse_vacancy_ids(html)

            if not page_ids:
                empty_reason = self._detect_empty_state(html)
                logger.info(
                    "[career] стр. %d пустая (html=%d байт, причина: %s)",
                    page, len(html), empty_reason,
                )
                break

            new_count = 0
            for vid in page_ids:
                if vid in seen:
                    continue
                seen.add(vid)
                all_ids.append(vid)
                new_count += 1

            if new_count == 0:
                logger.info(
                    "[career] стр. %d — новых вакансий нет, останавливаюсь",
                    page,
                )
                break

        logger.info(
            "[career] всего собрано вакансий (type=%s): %d",
            vacancy_type, len(all_ids),
        )
        return all_ids

    async def list_suitable_vacancies(
        self,
        *,
        max_pages: int = VACANCY_MAX_PAGES,
        fallback_to_all: bool = True,
    ) -> List[str]:
        """Подходящие вакансии с опциональным фоллбеком на ``type=all``.

        У свежезарегистрированного пользователя ``type=suitable`` часто
        пустой, пока career.habr.com не пересчитал рекомендации.
        """
        vacancies = await self.parse_vacancies(
            vacancy_type="suitable", max_pages=max_pages
        )
        if vacancies or not fallback_to_all:
            return vacancies

        logger.warning(
            "[career] suitable пустой — фоллбек на type=all (может быть много!)"
        )
        return await self.parse_vacancies(
            vacancy_type="all", max_pages=max_pages
        )

    @staticmethod
    def _detect_empty_state(html: str) -> str:
        """Пытается понять, почему выдача пустая."""
        soup = BeautifulSoup(html, "html.parser")
        for sel in (
            ".empty-layout",
            ".empty-list",
            ".vacancies-empty",
            "[data-qa='empty-state']",
        ):
            node = soup.select_one(sel)
            if node is not None:
                text = node.get_text(" ", strip=True)[:200]
                return f"{sel}: {text!r}"
        if not soup.select("div.vacancy-card"):
            return "нет ни одной div.vacancy-card"
        return "неизвестно"

    @staticmethod
    def parse_vacancy_ids(html: str) -> List[str]:
        """Парсит карточки вакансий. Поддерживает оба варианта разметки:
        со старым атрибутом ``data-vacancy-id`` и без него (SPA-рендер)."""
        soup = BeautifulSoup(html, "html.parser")
        ids: List[str] = []
        seen: set[str] = set()

        for card in soup.select("div.vacancy-card"):
            vacancy_id = card.get("data-vacancy-id")
            if not vacancy_id:
                link = card.select_one(
                    ".vacancy-card__backdrop-link, .vacancy-card__title-link, "
                    ".vacancy-card__icon-link, a[href*='/vacancies/']"
                )
                href = str(link.get("href", "")) if link else ""
                m = re.search(r"/vacancies/(\d+)", href)
                if m:
                    vacancy_id = m.group(1)

            if not vacancy_id:
                continue
            vacancy_id = str(vacancy_id)
            if vacancy_id in seen:
                continue
            seen.add(vacancy_id)
            ids.append(vacancy_id)

        return ids

    async def parse_vacancy(self, vacancy_id: str):
        """GET карточки вакансии и парсинг через ``extract_vacancy``."""
        url = f"{BASE}/vacancies/{vacancy_id}"
        async with self.session.get(url) as r:
            html = await r.text()
        vacancy = extract_vacancy(html)
        vacancy.link = url
        return vacancy

    async def respond_to_vacancy(
        self,
        vacancy_id: str,
        *,
        cover_letter: str = "",
    ) -> dict[str, Any]:
        """Откликается на одну вакансию: POST /responses + PATCH с body.

        Возможные исключения:

        * :class:`AlreadyAppliedError` — career ответил 401 (уже откликались);
        * :class:`RateLimitedError`    — career вернул 400 ``раз в 10 секунд``;
        * :class:`VacancyResponseError` — другая HTTP-ошибка (400/403/422/…),
          тело ответа доступно в ``.body``.
        """
        vacancy_url = f"{BASE}/vacancies/{vacancy_id}"
        csrf, _ = await self._career_page(vacancy_url)
        base_headers = self._ajax_headers(csrf, vacancy_url)

        # 1) Создаём отклик (пустой multipart)
        create_url = f"{BASE}/api/frontend/vacancies/{vacancy_id}/responses"
        boundary, empty_body = _build_empty_multipart()
        headers = {
            **base_headers,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        logger.info("[career] создаю отклик на #%s", vacancy_id)
        async with self.session.post(
            create_url, data=empty_body, headers=headers
        ) as r:
            create_status = r.status
            create_text = await r.text()

        if create_status == 401:
            logger.info(
                "[career] #%s — уже откликались, пропускаю", vacancy_id
            )
            raise AlreadyAppliedError(f"#{vacancy_id}")

        if create_status >= 400:
            body_snippet = create_text[:500].replace("\n", " ")
            logger.warning(
                "[career] POST /responses для #%s -> HTTP %d | body=%s",
                vacancy_id, create_status, body_snippet,
            )
            if create_status == 400 and _is_rate_limited(create_text):
                raise RateLimitedError(create_status, create_text)
            raise VacancyResponseError(create_status, create_text)

        try:
            import json as _json
            created = _json.loads(create_text)
        except ValueError as exc:
            raise VacancyResponseError(create_status, create_text) from exc

        response_id = (
            created.get("id")
            or (created.get("response") or {}).get("id")
        )
        if not response_id:
            raise VacancyResponseError(
                create_status,
                f"POST /responses не вернул id отклика: {created}",
            )
        logger.info("[career] отклик создан: id=%s", response_id)

        # 2) Отправляем текст сопроводительного письма
        patch_url = f"{create_url}/{response_id}"
        boundary, patch_body = _build_multipart_fields(
            [("body", cover_letter or "!")]
        )
        headers = {
            **base_headers,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        logger.info(
            "[career] PATCH body для отклика id=%s (длина письма=%d)",
            response_id, len(cover_letter or "!"),
        )
        async with self.session.patch(
            patch_url, data=patch_body, headers=headers
        ) as r:
            patch_status = r.status
            patch_text = await r.text()
        if patch_status >= 400:
            body_snippet = patch_text[:500].replace("\n", " ")
            logger.warning(
                "[career] PATCH /responses/%s -> HTTP %d | body=%s",
                response_id, patch_status, body_snippet,
            )
            raise VacancyResponseError(patch_status, patch_text)

        try:
            import json as _json
            patched = _json.loads(patch_text)
        except ValueError:
            patched = {"status_code": patch_status}
        logger.info("[career] отклик %s обновлён -> %s", response_id, patched)
        return patched

    async def respond_to_all(
        self,
        vacancy_ids: Iterable[str],
        *,
        cover_letter: str = "",
        delay_range: Tuple[float, float] = VACANCY_RESPONSE_DELAY,
    ) -> dict[str, Any]:
        """Откликается на все вакансии последовательно, с паузами.

        Возвращает статистику с ключами ``ok`` / ``already`` / ``failed``
        и список ``errors`` (``{vacancy, kind, error}``).
        """
        stats: dict[str, Any] = {
            "ok": 0,
            "already": 0,
            "failed": 0,
            "errors": [],
        }
        for vacancy_id in vacancy_ids:
            await self._apply_one_with_retry(vacancy_id, cover_letter, stats)
            pause = random.uniform(*delay_range)
            logger.debug(
                "[career] пауза перед следующим откликом: %.2fs", pause
            )
            await asyncio.sleep(pause)

        logger.info(
            "[career] рассылка завершена: ok=%d, already=%d, failed=%d",
            stats["ok"], stats["already"], stats["failed"],
        )
        return stats

    async def _apply_one_with_retry(
        self,
        vacancy_id: str,
        cover_letter: str,
        stats: dict[str, Any],
    ) -> bool:
        """Откликается на одну вакансию с ретраями по rate-limit."""
        for attempt in range(1, 4):
            try:
                await self.respond_to_vacancy(
                    vacancy_id, cover_letter=cover_letter
                )
                stats["ok"] += 1
                return True
            except AlreadyAppliedError:
                stats["already"] += 1
                return True
            except RateLimitedError:
                logger.info(
                    "[career] rate-limit на #%s, попытка %d — жду %.1fs",
                    vacancy_id, attempt, VACANCY_RATE_LIMIT_BACKOFF,
                )
                await asyncio.sleep(VACANCY_RATE_LIMIT_BACKOFF)
                continue
            except VacancyResponseError as exc:
                stats["failed"] += 1
                stats["errors"].append(
                    {
                        "vacancy": f"#{vacancy_id}",
                        "kind": f"http_{exc.status}",
                        "error": exc.body[:300],
                    }
                )
                logger.warning(
                    "[career] отклик на #%s провалился: %s", vacancy_id, exc
                )
                return False
            except Exception as exc:  # noqa: BLE001
                stats["failed"] += 1
                stats["errors"].append(
                    {
                        "vacancy": f"#{vacancy_id}",
                        "kind": "other",
                        "error": str(exc),
                    }
                )
                logger.exception(
                    "[career] ошибка отклика на #%s", vacancy_id
                )
                return False

        # все ретраи съел rate-limit
        stats["failed"] += 1
        stats["errors"].append(
            {
                "vacancy": f"#{vacancy_id}",
                "kind": "rate_limit",
                "error": "too many retries",
            }
        )
        logger.warning(
            "[career] #%s: rate-limit не ушёл за 3 попытки, скипаю",
            vacancy_id,
        )
        return False


# ---------------------------------------------------------------------------
# Top-level entrypoint для воркера
# ---------------------------------------------------------------------------


async def respond_to_vacancies(ctx) -> dict:
    """Откликнуться на все подходящие вакансии Хабр.Карьеры под юзера.

    Объединяет за один проход всё, что нужно сделать воркеру для одной
    итерации habr-площадки:

        1. Заходит на career.habr.com от лица юзера (берёт сохранённые
           cookies из БД через :class:`BaseJobSiteClient`).
        2. Проверяет, что онбординг пройден — если нет, ранний выход
           с ``error=onboarding_required``.
        3. Через ``list_suitable_vacancies`` собирает список подходящих
           вакансий (фоллбек на ``type=all`` при пустом ``suitable``).
        4. Для каждой вакансии: парсит карточку, для pro-юзеров генерирует
           AI cover-letter, отправляет отклик c корректной паузой между
           вызовами (career лимитирует ~1 отклик в 10с).

    Параметры
    ---------
    ctx : WorkerContext
        Снимок юзера: ``user_id``, тариф, ``resume_txt`` и т.п.

    Возвращает
    ----------
    dict с ключами:

    * ``scraped_vacancies``       — сколько вакансий удалось распарсить.
    * ``cover_letters_generated`` — сколько AI cover-letter'ов сгенерировано
      (всегда 0 для не-pro-тарифов).
    * ``responses``               — список ответов career.habr.com на
      каждый POST/PATCH (включая ошибки, для подсчёта и отладки).
    """
    scraped_vacancies = 0
    cover_letters_generated = 0
    responses: list[dict] = []

    async with HabrClient.from_context(ctx) as client:
        if not await client.is_authenticated():
            logger.warning(
                "[career] habr-сессия невалидна — нужен повторный логин"
            )
            return {
                "scraped_vacancies": scraped_vacancies,
                "cover_letters_generated": cover_letters_generated,
                "responses": responses,
                "error": "session_expired",
            }

        if await client.needs_onboarding():
            logger.warning(
                "[career] онбординг не пройден — отклики невозможны"
            )
            return {
                "scraped_vacancies": scraped_vacancies,
                "cover_letters_generated": cover_letters_generated,
                "responses": responses,
                "error": "onboarding_required",
            }

        vacancy_ids = await client.list_suitable_vacancies()
        if not vacancy_ids:
            logger.warning("[career] подходящих вакансий не нашлось")
            return {
                "scraped_vacancies": scraped_vacancies,
                "cover_letters_generated": cover_letters_generated,
                "responses": responses,
            }

        resume_txt = ctx.resume_txt

        for vacancy_id in vacancy_ids:
            try:
                vacancy = await client.parse_vacancy(vacancy_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[career] не смог распарсить #%s: %s", vacancy_id, exc
                )
                responses.append(
                    {"vacancy_id": vacancy_id, "error": f"parse: {exc}"}
                )
                continue

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
            try:
                result = await client.respond_to_vacancy(
                    vacancy_id=vacancy_id,
                    cover_letter=cover_letter,
                )
            except AlreadyAppliedError:
                result = {"already": True, "vacancy_id": vacancy_id}
            except RateLimitedError as exc:
                logger.info(
                    "[career] rate-limit на #%s, жду %.1fs",
                    vacancy_id, VACANCY_RATE_LIMIT_BACKOFF,
                )
                await asyncio.sleep(VACANCY_RATE_LIMIT_BACKOFF)
                result = {
                    "vacancy_id": vacancy_id,
                    "error": "rate_limited",
                    "status": exc.status,
                }
            except VacancyResponseError as exc:
                result = {
                    "vacancy_id": vacancy_id,
                    "error": str(exc),
                    "status": exc.status,
                }

            print(f"RESULT: {result}")
            responses.append(result)

            # Career ограничивает отклики ~1 раз в 10с — выдерживаем паузу.
            await asyncio.sleep(random.uniform(*VACANCY_RESPONSE_DELAY))

    return {
        "scraped_vacancies": scraped_vacancies,
        "cover_letters_generated": cover_letters_generated,
        "responses": responses,
    }


# ---------------------------------------------------------------------------
# CLI-вспомогалки для ручной регистрации/онбординга (test-only)
# ---------------------------------------------------------------------------


async def ensure_logged_in(
    client: HabrClient,
    *,
    email: str,
    password: str,
) -> None:
    register_link, page_token, sitekey = await client.discover_register_flow()

    register_response = await client.start_register(
        register_link=register_link,
        page_token=page_token,
        sitekey=sitekey,
        email=email,
        password=password,
    )

    if register_response.get("success"):
        code = await prompt(
            f"На адрес {email} отправлено письмо с кодом, введите его: "
        )
        finish = await client.finish_register(page_token, code)
        if not finish.get("success"):
            raise RuntimeError(
                f"Ошибка на последней стадии регистрации: {finish}"
            )
        logger.info("Регистрация успешно завершена!")
        return

    errors = register_response.get("errors") or {}
    if errors.get("email") != "Аккаунт с такой почтой уже зарегистрирован":
        raise RuntimeError(f"Регистрация не удалась: {register_response}")

    logger.info("Аккаунт уже существует, иду через логин")
    existing_password = await prompt(
        "Вы уже зарегистрированы на Habr, введите пароль: "
    )
    ok = await client.login(
        page_token=page_token,
        sitekey=sitekey,
        email=email,
        password=existing_password,
    )
    if not ok:
        raise RuntimeError("Логин на account.habr.com не удался")


async def run_career_onboarding(client: HabrClient) -> None:
    """Заполняет онбординг от резюме до /onboarding/complete."""
    resume_link = await prompt("Введите ссылку hh.ru резюме: ")
    await client.fetch_hh_resume(resume_link)
    await client.wait_hh_resume_ready()

    for category in PROFILE_CATEGORIES:
        await client.submit_profile_category(category)

    await client.complete_onboarding()


# ---------------------------------------------------------------------------
# Пример использования
# ---------------------------------------------------------------------------


async def main() -> None:
    setup_logging(level=logging.INFO)

    email = await prompt("email: ")
    async with HabrClient(email=email) as client:
        if not await client.is_authenticated():
            password = generate_password()
            logger.info("сгенерированный пароль: %s", password)
            await ensure_logged_in(client, email=email, password=password)
            await client.sso_to_career()

        if await client.needs_onboarding():
            await run_career_onboarding(client)
        else:
            logger.info("[career] онбординг уже пройден")


if __name__ == "__main__":
    asyncio.run(main())
