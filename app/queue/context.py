"""Контекст для воркеров: всё, что нужно знать про юзера на момент
запуска задачи.

Зачем отдельный модуль и зачем вообще dataclass, а не передавать
``User`` ORM-объект:

1. **ORM-объекты привязаны к сессии.** Если передать ``User`` из
   запроса/планировщика в ``asyncio.create_task`` (а в будущем — через
   arq в Redis), то к моменту выполнения сессии уже не будет: любая
   попытка прочитать `user.subscription` или `user.preferences` упадёт
   ``DetachedInstanceError``. Сериализация ORM-объекта в Redis тоже
   не работает (там relationship, lazy-loaders, метаданные).
2. **Данные могут устареть.** Между «положили в очередь» и «воркер
   взял в работу» проходит время — план юзера мог поменяться,
   подписка истечь, площадка отключиться. Если бы мы сохраняли
   снимок в момент enqueue, воркер делал бы решения по устаревшему
   состоянию.
3. **Воркер должен начинать с чистой свежей сессии БД.** Это
   позволяет ему открывать столько коротких транзакций, сколько
   нужно, без рисков «long-running transaction» с блокировками.

Поэтому контракт:

* В очередь кладётся **только** ``user_id`` (UUID — простой,
  сериализуемый, не устаревает).
* Воркер первым делом зовёт :func:`load_worker_context` — она
  одним SQL-запросом (с ``selectinload``) подгружает всё нужное:
  ``User`` + ``Subscription`` + ``Resume`` (для текста и настроек
  поиска, которые раньше жили в ``user_preferences``) + активные
  ``PlatformCredential``.
* Дальше воркер работает с **plain Python dataclass'ом**
  ``WorkerContext`` — у него нет привязки к сессии, его можно
  спокойно передавать вглубь (``application_sender(ctx)``,
  ``cover_letter_generator(ctx)``).

Если задаче нужен ещё какой-то срез данных (например, недавние
``ActionLog``-записи) — добавляешь поле в ``WorkerContext`` и
подгрузку в ``load_worker_context``, а не лезешь в БД из тела
воркера.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db.models.platform_credential import (
    PlatformCredential,
    STATUS_ACTIVE as PLATFORM_STATUS_ACTIVE,
)
from app.db.models.resume import Resume
from app.db.models.subscription import (
    PLAN_BASIC,
    PLAN_FREE,
    PLAN_PRO,
    Subscription,
)
from app.db.models.user import User
from app.db.session import session_scope
from app.services.job_sites.registry import filter_supported as filter_supported_platforms

logger = logging.getLogger("offerday.queue.context")


# ── Lazy-pull текста резюме ──────────────────────────────────────────


async def _read_or_pull_resume_txt(
    db,
    *,
    user_id: uuid.UUID,
    phone_number: str | None,
) -> str:
    """Достать ``Resume.full_txt`` из БД; если пусто — попробовать
    подтянуть с hh.ru и записать в БД.

    Возвращает строку (никогда ``None``). Если ничего не получили —
    отдаём пустую строку, и вызывающий код (`generate_cover_letter`,
    matcher и т.д.) сам решает, что делать.

    Поток:

    1. ``SELECT Resume.full_txt FROM resumes WHERE user_id = ?``.
       Если строки нет или поле пусто — переходим к шагу 2.
    2. Если у юзера нет ``phone_number`` (например, регистрировался
       через какой-то другой канал) — резюме hh.ru дёрнуть нечем,
       отдаём ``""``.
    3. Иначе зовём
       :func:`app.queue.jobs.pull_and_save_resume_for_user` —
       поднимает HHClient под локом, парсит, апсертит в ``resumes``,
       возвращает summary с ``full_txt``. Любая ошибка (нет cookies,
       hh.ru отвалился, резюме не нашли) → лог + ``""``.

    Импорт ``pull_and_save_resume_for_user`` сделан внутри функции,
    чтобы не создавать циклический импорт ``context → jobs → ...``
    на уровне модуля (``jobs`` тоже зависит от ``context``).
    """
    res = await db.execute(
        select(Resume.full_txt).where(Resume.user_id == user_id)
    )
    db_txt = res.scalar_one_or_none()
    if db_txt:
        return db_txt

    if not phone_number:
        logger.info(
            "resume_txt: пусто в БД и нет phone_number у user=%s — "
            "возвращаем пустую строку",
            user_id,
        )
        return ""

    # Локальный импорт: см. docstring выше.
    from app.queue.jobs import pull_and_save_resume_for_user
    from app.services.job_sites.hh import _to_hh_phone

    hh_phone = _to_hh_phone(phone_number)
    try:
        result = await pull_and_save_resume_for_user(
            user_id=user_id,
            hh_phone=hh_phone,
        )
    except Exception:
        logger.exception(
            "resume_txt: lazy-pull для user=%s упал — "
            "возвращаем пустую строку",
            user_id,
        )
        return ""

    return result.get("full_txt", "") or ""


# ── Лимиты по тарифам ────────────────────────────────────────────────
#
# Сколько откликов (откликов на вакансии) юзеру разрешено в день
# в рамках его подписки. ``None`` = безлимит.
#
# Эти числа — единственное место, где определяется «во сколько в
# день можно откликаться». Все воркеры (work_at_all, application_sender)
# должны брать лимит ИЗ ``WorkerContext.applications_per_day``, а не
# хардкодить тут или в своём коде.
#
# Когда заведём биллинг с custom-лимитами под конкретного юзера —
# добавится поле ``Subscription.applications_per_day_override`` и
# ``load_worker_context`` будет учитывать его.
APPLICATIONS_PER_DAY_BY_PLAN: dict[str, int | None] = {
    PLAN_FREE: 5,
    PLAN_BASIC: 50,
    PLAN_PRO: None,        # безлимит
}


# ── Снимок настроек юзера ────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class UserPrefsSnapshot:
    """Срез настроек юзера, нужный воркерам.

    После выпила таблицы ``user_preferences`` источники:

        * ``users``  — флаг ``auto_apply_running`` и новый
          ``auto_up_resume`` (пока никто не читает);
        * ``resumes.parsed_data`` — всё остальное (желаемая
          позиция, стек, зарплата, форматы работы).

    Стоп-слова/стоп-компании были редактируемыми полями в
    старой таблице — в резюме их нет, поэтому в снимке
    всегда ``None``. AI cover letter по умолчанию включён.
    """
    position: str | None
    grade: str | None
    stack: str | None
    salary: str | None
    work_formats: list[str] | None
    stop_companies: str | None
    stop_words: str | None
    profile_url: str | None
    ai_cover_letter_enabled: bool


# ── Снимок подписки ──────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class SubscriptionSnapshot:
    plan: str                          # free / basic / pro
    status: str                        # trial / active / expired / paused / cancelled
    expires_at: datetime | None        # None = бессрочная (free)


# ── Полный контекст ──────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class WorkerContext:
    """Снимок всего, что нужно знать воркеру про юзера.

    Создаётся :func:`load_worker_context`. Иммутабельный — если воркеру
    нужно «обновить» что-то, он:
        1. Сам пишет в БД через свою короткую транзакцию.
        2. По желанию — перевызывает ``load_worker_context``, чтобы
           получить свежий snapshot.
    """
    user_id: uuid.UUID
    phone_number: str | None
    is_active: bool

    subscription: SubscriptionSnapshot
    prefs: UserPrefsSnapshot
    resume_txt: str

    # Имена площадок (``"hh"``, ``"habr"``, …), на которых у юзера
    # **активная** учётка. То есть только те, через которые воркер
    # реально может постить отклики прямо сейчас. Если в БД учётка
    # есть, но статус ``error``/``expired``/``captcha_required``/
    # ``disabled`` — её сюда НЕ кладём.
    active_platforms: list[str] = field(default_factory=list)

    # Вычисляемое: сколько откликов в день у юзера по его тарифу.
    # ``None`` = безлимит (pro).
    applications_per_day: int | None = None

    # ── Удобные предикаты ───────────────────────────────────────────
    @property
    def is_pro(self) -> bool:
        return self.subscription.plan == PLAN_PRO

    @property
    def is_paid(self) -> bool:
        return self.subscription.plan in (PLAN_BASIC, PLAN_PRO)

    def has_platform(self, name: str) -> bool:
        return name in self.active_platforms


# ── Loader ───────────────────────────────────────────────────────────


async def load_worker_context(user_id: uuid.UUID) -> WorkerContext | None:
    """Загружает всё нужное про юзера ОДНИМ запросом и возвращает
    ``WorkerContext`` (или ``None``, если юзер не существует /
    помечен ``is_active=False``).

    Почему ``None`` для inactive: scheduler выбирает ``is_active=True``
    юзеров, но между tick'ом и стартом воркера юзер мог быть
    деактивирован (например, fraud-чек Stage-2 поставил
    ``is_active=False``). Мы НЕ хотим в этот момент гнать его дальше
    в логику откликов — поэтому воркер увидит ``None`` и сразу
    выйдет.

    Один SQL-запрос:
        SELECT users.*, subscriptions.*, resumes.parsed_data,
               platform_credentials.* (только active)
        FROM users
        LEFT JOIN subscriptions ON ...
        LEFT JOIN resumes ON ...
        LEFT JOIN platform_credentials ON ... AND status='active'
        WHERE users.id = ?

    SQLAlchemy через ``selectinload`` сделает это N+1-безопасно
    (одна вторичная выборка для коллекции ``credentials``,
    которая ограничена по ``status='active'``).
    """
    async with session_scope() as db:
        stmt = (
            select(User)
            .where(User.id == user_id)
            .options(
                selectinload(User.subscription),
                selectinload(User.credentials.and_(
                    PlatformCredential.status == PLATFORM_STATUS_ACTIVE,
                )),
            )
        )
        user = (await db.execute(stmt)).scalar_one_or_none()

        if user is None:
            logger.warning("load_worker_context: user %s not found", user_id)
            return None
        if not user.is_active:
            logger.info(
                "load_worker_context: user %s is inactive, skipping", user_id,
            )
            return None

        # Подписка — может быть None, если по какой-то причине её
        # не создали (fallback на free trial).
        sub = user.subscription
        sub_snapshot = SubscriptionSnapshot(
            plan=sub.plan if sub else PLAN_FREE,
            status=sub.status if sub else "trial",
            expires_at=sub.expires_at if sub else None,
        )

        # Настройки в основном живут в ``resumes.parsed_data`` —
        # достаём резюме юзера (должно быть не больше одной
        # строки) и вытаскиваем из его ``parsed_data`` нужные поля.
        resume_row = (
            await db.execute(
                select(Resume).where(Resume.user_id == user.id)
            )
        ).scalar_one_or_none()
        parsed: dict = (
            resume_row.parsed_data if resume_row and resume_row.parsed_data else {}
        )

        def _parsed_list(key: str) -> list[str] | None:
            value = parsed.get(key)
            if value is None:
                return None
            if isinstance(value, list):
                return [str(x) for x in value if x]
            return [str(value)]

        skills = parsed.get("skills") or []
        stack_str: str | None = None
        if skills:
            stack_str = ", ".join(str(s) for s in skills)

        # ``hh_resume_id`` отдельной колонкой не лежит — воркер кладёт
        # маркер ``"hh:<hash>"`` в ``Resume.file_path_pdf`` (см.
        # ``app.db.resumes.resume_to_db``). Для PDF-резюме в этом поле
        # просто путь к файлу, поэтому ``profile_url`` останется None.
        profile_url: str | None = None
        if resume_row is not None:
            fp = resume_row.file_path_pdf or ""
            if fp.startswith("hh:"):
                hh_hash = fp[len("hh:"):]
                if hh_hash:
                    profile_url = f"https://hh.ru/resume/{hh_hash}"

        prefs_snapshot = UserPrefsSnapshot(
            position=parsed.get("title") or None,
            grade=None,
            stack=stack_str,
            salary=parsed.get("salary") or None,
            work_formats=_parsed_list("work_format"),
            stop_companies=None,
            stop_words=None,
            profile_url=profile_url,
            ai_cover_letter_enabled=True,
        )

        # Платформы. Через ``selectinload(... .and_(status=active))``
        # уже отфильтрованы по статусу — берём slug'и. Дополнительно
        # фильтруем через registry: если в БД лежит legacy-площадка,
        # которой больше нет в коде, воркер о ней знать не должен.
        active_platforms = filter_supported_platforms(
            sorted(c.platform for c in user.credentials)
        )

        # ── Текст резюме ────────────────────────────────────────────
        # Источник №1 — БД (``Resume.full_txt``). Если пусто (юзер
        # ещё не дёргал парсер, либо парсер прошёл, но raw_text не
        # сохранил) — пробуем подтянуть с hh.ru прямо здесь, через
        # ``pull_and_save_resume_for_user``. Это синхронный fallback
        # под кейс «воркер запустился раньше, чем парсинг резюме»;
        # обычно на нём не оказываемся, потому что для новых
        # юзеров `enqueue_resume_parse` отрабатывает в verify-code.
        resume_txt = await _read_or_pull_resume_txt(
            db,
            user_id=user.id,
            phone_number=user.phone_number,
        )

        # Вычисляем дневной лимит.
        applications_per_day = APPLICATIONS_PER_DAY_BY_PLAN.get(
            sub_snapshot.plan, APPLICATIONS_PER_DAY_BY_PLAN[PLAN_FREE]
        )

        return WorkerContext(
            user_id=user.id,
            phone_number=user.phone_number,
            is_active=user.is_active,
            subscription=sub_snapshot,
            prefs=prefs_snapshot,
            resume_txt=resume_txt,
            active_platforms=active_platforms,
            applications_per_day=applications_per_day,
        )
