"""Helpers around the :class:`User` row that backs an HTTP session.

The hh.ru login flow in :mod:`app.api.auth` only stores the bare
minimum on the session (phone, name, hh user id).  When the user hits
a dashboard page we look up the corresponding :class:`User` row by
``phone_number``.

:func:`ensure_user_for_session` is a **read-only** lookup: returns the
existing :class:`User` if present, otherwise ``None``. Создание новых
юзеров — исключительно в ``/auth/verify-code`` после успешной OTP-
проверки.

Дополнительно тут живёт ``PrefsView`` — read-only «срез» настроек
юзера, который имитирует прежний ``UserPreferences``-объект. Сама
таблица ``user_preferences`` ушла; имя/желаемая позиция/скиллы и
прочее теперь читаются из последней строки ``resumes`` юзера, а
три флага (``auto_apply_running`` / ``notif_email`` /
``notif_telegram``) переехали столбцами на ``users``. ``PrefsView``
просто дёргает свойства из ``User`` + ``Resume.parsed_data`` и
возвращает их в шейпе, ожидаемом старым кодом (`prefs.position`,
`prefs.first_name`, и т.д.) — чтобы шаблоны и dashboard_data не
пришлось переписывать построчно.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Resume, Subscription, User
from app.services.platform_password import generate_platform_password
from app.services.temp_mail import create_temp_email


def _synthetic_email_for_phone(e164_phone: str) -> str:
    """Build a unique placeholder email from a phone number.

    hh.ru does not necessarily expose the user's real email, but our
    schema declares ``users.email`` as ``unique not null``.  Until the
    user fills in a real address in settings we use a synthetic one
    derived from the phone, which is guaranteed unique per account.
    """
    digits = "".join(c for c in e164_phone if c.isdigit())
    return f"{digits or 'unknown'}@hh.local"


async def find_by_phone(
    session: AsyncSession, e164_phone: str,
) -> User | None:
    """Возвращает существующего ``User`` по телефону или ``None``."""
    if not e164_phone:
        return None
    stmt = select(User).where(User.phone_number == e164_phone)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_user_for_phone(
    session: AsyncSession,
    *,
    e164_phone: str,
    first_name: str = "",  # noqa: ARG001 — legacy сигнатура
    last_name: str = "",   # noqa: ARG001
) -> User:
    """Создаёт нового ``User`` под этот телефон + дефолтную ``Subscription``.

    Использовать только когда :func:`find_by_phone` уже вернул
    ``None``: повторное создание упадёт на unique-ограничении
    ``users.phone_number``. Транзакцию НЕ коммитит — за это отвечает
    вызывающий (FastAPI dependency ``get_db`` / ``session_scope``).

    ``first_name`` / ``last_name`` параметры сохранены для обратной
    совместимости с прежним API — раньше они уезжали в
    ``user_preferences``. Сейчас имена приходят из парсенного резюме,
    поэтому аргументы игнорируются.

    Дополнительно:

    * Выдаём пользователю временный почтовый ящик из ``temp.coda.ink``
      (см. :func:`app.services.temp_mail.create_temp_email`). Сейчас
      это ЗАГЛУШКА — генерируется локально без реального запроса к API;
      когда добавим вызов, эта функция оставит сигнатуру.
    * Генерируем детерминированный «платформенный» пароль из
      ``user.id`` + соли и сохраняем его в ``users.platform_password``.
      Соль не ротируем, поэтому хранение нужно ровно для одной цели —
      показать пользователю значение в ``/settings`` без пересчёта
      каждый запрос (и зафиксировать его, даже если соль однажды
      придётся аварийно сменить).
    """
    user = User(
        phone_number=e164_phone,
        email=_synthetic_email_for_phone(e164_phone),
        password_hash="",
    )
    session.add(user)
    await session.flush()  # populate user.id

    # ── Платформенные доступы (для ручного входа на площадки) ──────
    # ВАЖНО: пароль генерируем СТРОГО после flush — нам нужен ``user.id``.
    user.platform_password = generate_platform_password(user.id)
    try:
        mailbox = await create_temp_email()
        user.temp_email = mailbox.email
        user.temp_email_token = mailbox.token
    except Exception:
        # Если temp.coda.ink временно недоступен — НЕ ломаем регистрацию.
        # Поле дозаполнится лениво при первом заходе на /settings через
        # :func:`ensure_platform_credentials`.
        user.temp_email = None
        user.temp_email_token = None
    await session.flush()

    session.add(
        Subscription(
            user_id=user.id,
            plan="free",
            status="trial",
        )
    )
    await session.flush()
    return user


async def ensure_platform_credentials(
    session: AsyncSession,
    user: User,
) -> User:
    """Дозалить ``temp_email`` / ``platform_password`` для существующего юзера.

    Нужно для бесшовной миграции на новую схему: те, кто
    зарегистрировался до выкатки этой фичи, не имеют ни временной почты,
    ни сгенерированного пароля. При первом заходе на ``/settings`` (или
    при попытке подключить habr) — дозальём.

    Идемпотентна: ничего не делает, если оба поля уже заполнены.
    Не коммитит транзакцию — вызывающий сам решает, когда коммитить.
    """
    changed = False
    if not user.platform_password:
        user.platform_password = generate_platform_password(user.id)
        changed = True
    if not user.temp_email:
        try:
            mailbox = await create_temp_email()
            user.temp_email = mailbox.email
            user.temp_email_token = mailbox.token
            changed = True
        except Exception:
            # Если сервис временной почты лёг — оставляем None и
            # попробуем в следующий раз.
            pass
    if changed:
        await session.flush()
    return user


async def ensure_user_for_session(
    session: AsyncSession,
    session_user: dict[str, Any],
) -> User | None:
    """Найти :class:`User` для текущей HTTP-сессии. **Без авто-создания.**

    Возвращает существующего юзера либо ``None``. Вызывающие коды на
    ``None`` чистят сессию и редиректят на /login.
    """
    phone = session_user.get("phone") or ""

    user = await find_by_phone(session, phone)
    if user is not None:
        if user.phone_number is None and phone:
            user.phone_number = phone
            await session.flush()
        return user

    email = _synthetic_email_for_phone(phone)
    legacy = (
        await session.execute(select(User).where(User.email == email))
    ).scalar_one_or_none()
    if legacy is not None:
        if legacy.phone_number is None and phone:
            legacy.phone_number = phone
            await session.flush()
        return legacy

    return None


# ── PrefsView ──────────────────────────────────────────────────────
#
# Read-only proxy, эмулирующий старый ``UserPreferences``. Заменяет
# ``get_or_create_preferences`` во всех чтениях. Шаблоны/loader'ы
# обращались к нему как ``prefs.position`` / ``prefs.first_name`` /
# ``prefs.work_formats``; PrefsView отдаёт ровно то же, но из
# ``User`` + последней строки ``resumes`` юзера (``parsed_data``
# JSONB). Поля, для которых источника больше нет (``grade``,
# ``stop_words``, ``language``, ``timezone_name``, …), возвращают
# дефолты (``None`` / ``""`` / ``False``) — на UI это смотрится
# как «пустой инпут».
#
# Если код где-то писал в prefs (`prefs.position = ...`), его
# нужно переписать или удалить — сеттеров тут нет.


def _split_full_name(full_name: str | None) -> tuple[str, str]:
    name = (full_name or "").strip()
    if not name:
        return "", ""
    parts = name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _hh_hash_from_resume(resume: "Resume | None") -> str | None:
    """Достаёт hh-hash из ``Resume.file_path_pdf``.

    Колонки ``hh_resume_id`` нет — воркер кладёт маркер
    ``"hh:<hash>"`` в ``file_path_pdf`` (см. ``app.db.resumes.resume_to_db``).
    Для PDF-резюме там лежит обычный путь — возвращаем None.
    """
    if resume is None:
        return None
    fp = resume.file_path_pdf or ""
    if fp.startswith("hh:"):
        return fp[len("hh:"):] or None
    return None


@dataclass(slots=True)
class PrefsView:
    """Снимок настроек юзера в шейпе старого ``UserPreferences``.

    Конструируется через :func:`get_user_prefs_view`. Read-only.
    """

    user: User
    resume: Resume | None

    # ── 3 столбца, реально хранящихся на ``users`` ─────────────────
    @property
    def auto_apply_running(self) -> bool:
        return bool(getattr(self.user, "auto_apply_running", False))

    @property
    def notif_email(self) -> bool:
        return bool(getattr(self.user, "notif_email", False))

    @property
    def notif_telegram(self) -> bool:
        return bool(getattr(self.user, "notif_telegram", False))

    # ── Поля, читаемые из ``resumes.parsed_data`` ──────────────────
    @property
    def _parsed(self) -> dict[str, Any]:
        if self.resume is None:
            return {}
        return self.resume.parsed_data or {}

    @property
    def first_name(self) -> str:
        return _split_full_name(self._parsed.get("full_name"))[0]

    @property
    def last_name(self) -> str:
        return _split_full_name(self._parsed.get("full_name"))[1]

    @property
    def contact_email(self) -> str | None:
        return self._parsed.get("email") or None

    @property
    def contact_phone(self) -> str | None:
        return self._parsed.get("phone") or self.user.phone_number

    @property
    def position(self) -> str | None:
        return self._parsed.get("title") or None

    @property
    def stack(self) -> str | None:
        skills = self._parsed.get("skills") or []
        if not skills:
            return None
        return ", ".join(str(s) for s in skills)

    @property
    def salary(self) -> str | None:
        return self._parsed.get("salary") or None

    @property
    def about_me(self) -> str | None:
        return self._parsed.get("summary") or None

    @property
    def work_formats(self) -> list[str]:
        wf = self._parsed.get("work_format")
        if not wf:
            return []
        if isinstance(wf, list):
            return [str(x) for x in wf if x]
        return [str(wf)]

    @property
    def profile_url(self) -> str | None:
        hh_hash = _hh_hash_from_resume(self.resume)
        if hh_hash:
            return f"https://hh.ru/resume/{hh_hash}"
        return None

    # ── «Мёртвые» поля старой схемы — источника нет, отдаём дефолты ─
    grade: str | None = None
    stop_companies: str | None = None
    stop_words: str | None = None
    language: str | None = None
    currency: str | None = None
    timezone_name: str | None = None
    ai_cover_letter_enabled: bool = True
    notif_push: bool = False
    notif_messages: bool = False
    notif_weekly: bool = False
    two_factor_enabled: bool = False
    telegram_link_token: str | None = None
    pending_platform_otps: dict[str, str] | None = None


async def get_user_prefs_view(
    session: AsyncSession,
    user: User,
) -> PrefsView:
    """Собирает :class:`PrefsView` для юзера, ОДНИМ запросом дотянув
    его последнее резюме (если есть).

    Замена прежней ``get_or_create_preferences`` — больше не создаёт
    никаких строк, потому что таблицы ``user_preferences`` нет, а
    три флага (``auto_apply_running``/``notif_email``/
    ``notif_telegram``) уже лежат прямо на ``User``.
    """
    resume = (
        await session.execute(select(Resume).where(Resume.user_id == user.id))
    ).scalar_one_or_none()
    return PrefsView(user=user, resume=resume)


# Backwards-compat alias.  Старые места вызова (``get_or_create_preferences``)
# теперь получают тот же ``PrefsView``-объект; писать в него нельзя
# (свойства read-only), но 3 переехавших флага можно менять прямо на
# ``user``, который держится в ``view.user``.
get_or_create_preferences = get_user_prefs_view
