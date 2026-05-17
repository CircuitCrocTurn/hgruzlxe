"""Базовый класс для клиентов площадок + механизм авто-регистрации.

Идея:
    1. Каждый клиент площадки наследуется от ``BaseJobSiteClient``.
    2. У него есть классовый атрибут ``slug`` — строгое строковое
       имя площадки (см. константы в
       :mod:`app.services.job_sites.registry`).
    3. ``__init_subclass__`` ловит создание подкласса и записывает его
       в общую мапу ``_CLIENTS_BY_SLUG``.
    4. Из воркера слаги достаются через ``ctx.active_platforms``,
       а под каждый — ``get_client_class(slug)`` отдаёт нужный класс.

То есть **дублирующего списка нигде нет**: реестр UI лежит в
``registry.py``, а инвентарь клиентов формируется автоматически
тем фактом, что модуль с клиентом импортирован (см.
``app/services/job_sites/__init__.py``).

Чтобы добавить новую площадку в код:

.. code-block:: python

    # app/services/job_sites/habr/client.py
    from app.services.job_sites.base import BaseJobSiteClient
    from app.services.job_sites.registry import PLATFORM_HABR

    class HabrClient(BaseJobSiteClient):
        slug = PLATFORM_HABR

        async def search(self, query):
            ...
        async def apply(self, vacancy):
            ...

И в ``app/services/job_sites/__init__.py`` дописать импорт — без
этого Python не выполнит ``class HabrClient`` и регистрация не
произойдёт. Это единственное «ручное» место — но это **импорт
модуля**, а не дублирование slug'а.

──────────────────────────────────────────────────────────────────────
Хранение сессии (cookies, csrf-токены, …)
──────────────────────────────────────────────────────────────────────

Раньше клиенты сохраняли cookies в pickle-файл на диске
(``var/hh_sessions/session_<phone>.pkl``). Теперь — в БД в столбце
``platform_credentials.encrypted_session``. Подробности см. в
:mod:`app.db.platform_credentials`.

На уровне базового класса есть два метода для подклассов:

* ``await self.save_session(user_id=..., blob=b"...")`` — записать
  blob в БД (по дороге зашифровать).
* ``await self.load_session(user_id=...)`` — достать blob (по дороге
  расшифровать). Возвращает ``None``, если сессии нет.

Что внутри blob'а — личное дело подкласса. Для hh это
``pickle.dumps(list(cookie_jar))``.
"""
from __future__ import annotations

import abc
import base64
import hashlib
import logging
from contextlib import asynccontextmanager
from typing import Any, ClassVar

from cryptography.fernet import Fernet, InvalidToken

from app.config import SESSION_SECRET

logger = logging.getLogger("offerday.job_sites.base")


# Внутренняя мапа slug → класс. Заполняется автоматически через
# ``__init_subclass__``. Снаружи использовать
# :func:`get_client_class` / :func:`all_registered_slugs`.
_CLIENTS_BY_SLUG: dict[str, type["BaseJobSiteClient"]] = {}


# ── Шифрование blob'ов сессии ─────────────────────────────────────────
#
# Fernet требует 32 URL-safe base64 байта. SESSION_SECRET у нас —
# произвольная строка (по умолчанию 'dev-only-secret-change-me-...'),
# и напрямую её скормить Fernet'у нельзя — он упадёт с
# ``ValueError: Fernet key must be 32 url-safe base64-encoded bytes``.
# Поэтому делаем детерминированную деривацию: SHA-256(SESSION_SECRET)
# → 32 байта → url-safe base64. Ключ один и тот же между рестартами,
# blob, записанный сегодня, прочитается завтра.
def _derive_fernet_key(secret: str) -> bytes:
    digest = hashlib.sha256(secret.encode("utf-8")).digest()  # 32 байта
    return base64.urlsafe_b64encode(digest)


_FERNET = Fernet(_derive_fernet_key(SESSION_SECRET))


def _encrypt(blob: bytes) -> bytes:
    """Зашифровать сырые байты сессии перед записью в БД."""
    return _FERNET.encrypt(blob)


def _decrypt(encrypted: bytes) -> bytes | None:
    """Расшифровать байты сессии из БД. ``None`` при поломанном blob'е."""
    try:
        return _FERNET.decrypt(encrypted)
    except InvalidToken:
        logger.warning(
            "platform_credentials.encrypted_session invalid token — "
            "ключ Fernet'а изменился? blob будет проигнорирован"
        )
        return None


class BaseJobSiteClient(abc.ABC):
    """Базовый класс клиента площадки.

    Подкласс обязан переопределить:

    * ``slug`` — идентификатор площадки;
    * ``from_context(cls, ctx)`` — фабрика инстанса из
      :class:`WorkerContext` (у каждой площадки своя схема
      авторизации: phone-OTP, API-token, OAuth, ...).

    Опционально подкласс может переопределить:

    * ``open_for(cls, ctx)`` — async context manager. Используется
      воркером как ``async with cls.open_for(ctx) as client``.
      Базовая реализация просто делает ``from_context`` +
      ``async with instance``. Переопределяй, если нужны локи /
      pre-flight проверки.
    * ``search(self, prefs)`` / ``apply(self, vacancy)`` /
      ``get_resume_id(self)`` — доменные методы. Все клиенты
      разделяют одинаковые сигнатуры. Если площадка чего-то не
      умеет — наследует дефолт (бросает ``NotImplementedError``),
      воркер ловит и пропускает.
    """

    #: Строковый идентификатор площадки. Должен совпадать с одной из
    #: констант в :mod:`app.services.job_sites.registry`.
    slug: ClassVar[str] = ""

    # ── Фабрика ─────────────────────────────────────────────────────
    # ОБЯЗАНА быть переопределена (у каждой площадки своя схема
    # авторизации — phone/token/OAuth).
    @classmethod
    def from_context(cls, ctx):  # noqa: D401
        raise NotImplementedError(
            f"{cls.__qualname__}: переопредели classmethod from_context(cls, ctx). "
            "См. примеры в HHClient.from_context (phone-based) или в "
            "docstring модуля."
        )

    # ── Контекст-менеджер ───────────────────────────────────────────
    # ОДИН на всех. Переопределяй только если нужны локи / pre-flight
    # проверки. Дефолт = from_context + async with.
    @classmethod
    @asynccontextmanager
    async def open_for(cls, ctx):
        instance = cls.from_context(ctx)
        async with instance as client:
            yield client

    # ── Доменный API (опциональные методы) ──────────────────────────
    # Все клиенты разделяют одинаковые сигнатуры. Если площадка
    # чего-то не умеет — наследует дефолт (NotImplementedError),
    # воркер ловит и пропускает.
    # async def get_resume_id(self) -> dict: raise NotImplementedError(...)
    # async def search(self, prefs) -> list: raise NotImplementedError(...)
    # async def apply(self, vacancy, *, cover_letter=None) -> dict:
    #     raise NotImplementedError(...)

    # ── Сессия (cookies, csrf-токены, …) ────────────────────────────
    async def save_session(self, *, user_id, blob: bytes) -> None:
        """Сохранить сериализованную сессию в БД под (user_id, self.slug).

        Что такое ``blob`` — личное дело подкласса. Для ``HHClient``
        это ``pickle.dumps(list(cookie_jar))``. Перед записью blob
        шифруется ключом, выведенным из ``SESSION_SECRET``.

        Параметр ``user_id`` обязателен: до создания строки в
        ``users`` сессии класть некуда (этот период покрывается
        in-memory bridge'ом в :mod:`app.services.job_sites.hh.hh`).
        """
        if not self.slug:
            raise RuntimeError(
                f"{type(self).__qualname__}: пустой slug — save_session невозможен"
            )
        # Импорт внутри метода, чтобы не было циклического импорта на
        # уровне модуля (app.db -> ... -> app.services.job_sites).
        from app.db.platform_credentials import save_session_blob

        encrypted = _encrypt(blob)
        await save_session_blob(
            user_id=user_id,
            platform=self.slug,
            encrypted_blob=encrypted,
        )

    async def load_session(self, *, user_id) -> bytes | None:
        """Достать сериализованную сессию из БД (расшифрованную).

        Возвращает ``None``, если сессии нет или blob битый (например,
        ключ Fernet'а изменился между деплоями — тогда blob трактуется
        как протухший и подкласс должен пройти полный логин).
        """
        if not self.slug:
            raise RuntimeError(
                f"{type(self).__qualname__}: пустой slug — load_session невозможен"
            )
        from app.db.platform_credentials import load_session_blob

        encrypted = await load_session_blob(
            user_id=user_id, platform=self.slug,
        )
        if encrypted is None:
            return None
        return _decrypt(encrypted)

    # ── Авто-регистрация подклассов ─────────────────────────────────
    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Авто-регистрация подкласса в общей мапе.

        Если подкласс **абстрактный** (промежуточный, без своего
        ``slug``) — не регистрируем. Это позволяет иметь
        промежуточные миксины ``CookieBasedClient(BaseJobSiteClient)``
        без фиктивного slug'а.
        """
        super().__init_subclass__(**kwargs)
        slug = getattr(cls, "slug", "") or ""
        if not slug:
            # Промежуточный класс без slug — пропускаем.
            return
        if slug in _CLIENTS_BY_SLUG and _CLIENTS_BY_SLUG[slug] is not cls:
            # Двойная регистрация одного и того же slug'а разными
            # классами — почти наверняка опечатка / merge-конфликт.
            existing = _CLIENTS_BY_SLUG[slug].__qualname__
            raise RuntimeError(
                f"duplicate JobSiteClient registration for slug={slug!r}: "
                f"{existing} vs {cls.__qualname__}"
            )
        _CLIENTS_BY_SLUG[slug] = cls


def get_client_class(slug: str) -> type[BaseJobSiteClient] | None:
    """Возвращает класс клиента для площадки или ``None``.

    ``None`` означает «реестр UI знает про эту площадку, но в коде
    клиента ещё нет» — например, ``LinkedIn`` в каталоге есть, а
    ``LinkedinClient`` пока не написан. Воркер должен это уметь
    обрабатывать (пропуск площадки + лог).
    """
    return _CLIENTS_BY_SLUG.get(slug)


def all_registered_slugs() -> list[str]:
    """Slug'и всех импортированных клиентов. Удобно для self-check
    на старте приложения: сравнить с ``SUPPORTED_PLATFORM_SLUGS`` и
    пожаловаться, если в каталоге UI есть площадка, под которую нет
    кода (или наоборот)."""
    return sorted(_CLIENTS_BY_SLUG)
