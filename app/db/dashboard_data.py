"""Data loaders for the four authenticated dashboard pages.

Each function returns the dict that the matching Jinja template
expects.  Where the database has nothing yet we still return an
empty dict / list so the template's ``{{ stats.x | default(0) }}``
expressions and ``{% for %}`` loops collapse to a friendly "0" or
"0 откликов" placeholder — the surrounding cards, charts and lists
still render exactly as the Variant design specifies them.

Schemas not yet modelled in :mod:`app.db.models` (applications,
conversations, telegram-vacancies) appear as TODO comments below — when
they land, plug them in and the templates will pick them up
automatically with no template changes required.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

# Все «человекочитаемые» даты в UI отдаём в московском часовом поясе —
# это и наша целевая аудитория, и hh.ru тоже отдаёт ``+03:00``. На
# фронте, если у юзера в браузере другая TZ, JS поверх перерисует время
# в локальную из ``data-iso`` атрибутов (см. responses.html / chats.html).
_MSK = ZoneInfo("Europe/Moscow")

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    ActionLog,
    Chat,
    Message,
    PlatformCredential,
    Resume,
    Subscription,
    User,
    VacancyResponse,
)
from app.db.users import PrefsView, ensure_platform_credentials, get_user_prefs_view as get_or_create_preferences
from app.services.job_sites.habr.chats_sync import _html_to_plain as _habr_html_to_plain

# ── Daily response quota per plan ──────────────────────────────────────
# Mirrors the limits shown on the /tariffs cards: free = 30 / day,
# Pro (slug "basic") = 100 / day, Premium (slug "pro") = unlimited.
# When you change the cards, change this map too.  ``None`` means
# "no daily cap".
PLAN_DAILY_LIMITS: dict[str, int | None] = {
    "free": 30,
    "basic": 100,
    "pro": None,
}


async def _quota_context(
    db: AsyncSession,
    user: User,
) -> dict[str, Any]:
    """Compute the "Отклики X / Y" sidebar widget data for ``user``.

    X = number of отправленных откликов (``vacancy_responses.is_sent =
        TRUE``) с полуночи МСК сегодня. Берём именно ``vacancy_responses``
        — это тот же источник, по которому строится «Последние отклики» и
        страница /responses, поэтому счётчик всегда консистентен с
        тем, что юзер видит в списке.
    Y = the daily cap that comes with the user's current subscription plan
        (``None`` for the unlimited Pro plan, displayed as ``∞``).
    """
    sub = (
        await db.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one_or_none()
    plan_slug = (sub.plan if sub else "free") or "free"
    limit = PLAN_DAILY_LIMITS.get(plan_slug, PLAN_DAILY_LIMITS["free"])

    # «Сегодня» считаем по московскому полуночи (лимит обновляется в
    # 00:00 МСК — это видно в подписи «Лимит обновляется в 00:00»).
    # Конвертируем границу в UTC для фильтра по ``created_at``
    # (Postgres хранит её как ``timestamptz``).
    now_msk = datetime.now(_MSK)
    today_start_utc = now_msk.replace(
        hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    used_today = (
        await db.execute(
            select(func.count(VacancyResponse.id)).where(
                VacancyResponse.user_id == user.id,
                VacancyResponse.is_sent.is_(True),
                VacancyResponse.created_at >= today_start_utc,
            )
        )
    ).scalar() or 0

    if limit is None:
        status = "Безлимит на тарифе Pro."
    elif used_today >= limit:
        status = "Дневной лимит исчерпан."
    else:
        status = "Лимит обновляется в 00:00."

    return {
        "responses_used_today": int(used_today),
        "responses_daily_limit": limit,
        "responses_daily_limit_label": "∞" if limit is None else str(limit),
        "responses_quota_status": status,
        "current_plan_slug": plan_slug,
    }


async def _weekly_responses(
    db: AsyncSession,
    user: User,
) -> dict[str, Any]:
    """Per-day "applications sent" counts for the last 7 days.

    Источник — ``vacancy_responses`` (тот же, что и счётчик «Откликов
    сегодня» / страница /responses). Берём отправленные строки
    (``is_sent = TRUE``) за текущую ISO-неделю в МСК — так бары в
    «Динамика откликов» совпадают с тем, что юзер видит у себя в
    /responses.
    """
    now_msk = datetime.now(_MSK)
    today_start_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    monday_msk = today_start_msk - timedelta(days=today_start_msk.weekday())
    next_monday_msk = monday_msk + timedelta(days=7)
    monday_utc = monday_msk.astimezone(timezone.utc)
    next_monday_utc = next_monday_msk.astimezone(timezone.utc)

    rows = (
        await db.execute(
            select(VacancyResponse.created_at).where(
                VacancyResponse.user_id == user.id,
                VacancyResponse.is_sent.is_(True),
                VacancyResponse.created_at >= monday_utc,
                VacancyResponse.created_at < next_monday_utc,
            )
        )
    ).scalars().all()

    counts = [0, 0, 0, 0, 0, 0, 0]
    for created_at in rows:
        # Postgres хранит ``created_at`` как ``timestamptz`` — приводим
        # к МСК, чтобы день недели бил с границей понедельник 00:00 МСК.
        created_msk = (
            created_at.astimezone(_MSK)
            if created_at.tzinfo is not None
            else created_at.replace(tzinfo=timezone.utc).astimezone(_MSK)
        )
        delta = (created_msk - monday_msk).days
        if 0 <= delta < 7:
            counts[delta] += 1

    today_idx = today_start_msk.weekday()
    labels = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
    max_count = max(counts) if counts else 0
    days = []
    for i, count in enumerate(counts):
        # Bar height as percent of the chart area.  When everything is
        # zero the bars are still rendered with 0% height so the labels
        # line up neatly under the empty grid.
        pct = round((count / max_count) * 92) if max_count else 0
        days.append(
            {
                "label": labels[i],
                "count": count,
                "height_pct": pct,
                "is_today": i == today_idx,
                "is_future": i > today_idx,
            }
        )
    total = sum(counts)
    # Average is over the days that have already happened this week
    # (including today), not all 7 days, so it doesn't get crushed by
    # weekend zeros at Monday lunchtime.
    elapsed = today_idx + 1
    avg = round(total / elapsed) if elapsed > 0 else 0
    return {
        "weekly_responses": {
            "days": days,
            "total": total,
            "avg_per_day": avg,
            "max": max_count,
            "has_data": total > 0,
        }
    }


# Display metadata for the «Площадки» card on /dashboard.  Keys here
# match what the worker writes into ``ActionLog.details['platform']``.
_PLATFORM_DISPLAY: dict[str, dict[str, str]] = {
    "hh":              {"name": "hh.ru",         "icon_text": "HH", "icon_bg": "bg-red-50",     "icon_text_color": "text-red-600",     "bar_class": "bg-red-400"},
    "linkedin":        {"name": "LinkedIn",      "icon_text": "LI", "icon_bg": "bg-blue-50",    "icon_text_color": "text-blue-600",    "bar_class": "bg-blue-400"},
    "upwork":          {"name": "Upwork",        "icon_text": "UP", "icon_bg": "bg-emerald-50", "icon_text_color": "text-emerald-600", "bar_class": "bg-emerald-400"},
    "otta":            {"name": "Otta",          "icon_text": "OT", "icon_bg": "bg-zinc-100",   "icon_text_color": "text-zinc-700",    "bar_class": "bg-zinc-400"},
    "habr":            {"name": "Habr Career",   "icon_text": "HB", "icon_bg": "bg-cyan-50",    "icon_text_color": "text-cyan-700",    "bar_class": "bg-cyan-400"},
    "github_jobs":     {"name": "GitHub Jobs",   "icon_text": "GH", "icon_bg": "bg-zinc-900",   "icon_text_color": "text-white",       "bar_class": "bg-zinc-700"},
    "telegram_jobs":   {"name": "Telegram Jobs", "icon_text": "TG", "icon_bg": "bg-yellow-50",  "icon_text_color": "text-yellow-700",  "bar_class": "bg-yellow-400"},
    "moi_krug":        {"name": "Moi Krug",      "icon_text": "MO", "icon_bg": "bg-zinc-100",   "icon_text_color": "text-zinc-700",    "bar_class": "bg-zinc-400"},
    "indeed":          {"name": "Indeed",        "icon_text": "IN", "icon_bg": "bg-indigo-50",  "icon_text_color": "text-indigo-700",  "bar_class": "bg-indigo-400"},
}


async def _platform_response_stats(
    db: AsyncSession,
    user: User,
) -> list[dict[str, Any]]:
    """Per-platform доля откликов за последние 7 дней.

    Источник — ``vacancy_responses`` (то же, что и страница /responses
    и счётчик «Откликов сегодня»). По каждой площадке считаем:

    * ``sent``      — сколько откликов ушло (``is_sent = TRUE``);
    * ``share_pct`` — доля площадки от **всех** отправленных откликов
                      этого юзера за 7 дней, округлённая до целого.

    Площадка берётся из ``vacancy_responses.service``. Площадки без
    отправок за 7 дней просто не попадают в результат — карточка не
    рисуется. Сортировка — по ``share_pct`` по убыванию (равные
    проценты — по объёму ``sent``, затем по имени для стабильности).
    """
    since = datetime.now(timezone.utc) - timedelta(days=7)
    rows = (
        await db.execute(
            select(VacancyResponse.service).where(
                VacancyResponse.user_id == user.id,
                VacancyResponse.is_sent.is_(True),
                VacancyResponse.created_at >= since,
            )
        )
    ).all()

    by_platform: dict[str, int] = {}
    total_sent = 0
    for (service,) in rows:
        platform = (service or "").strip().lower()
        if not platform:
            continue
        by_platform[platform] = by_platform.get(platform, 0) + 1
        total_sent += 1

    out: list[dict[str, Any]] = []
    for slug, sent in by_platform.items():
        meta = _PLATFORM_DISPLAY.get(slug)
        if meta is None:
            # Unknown platform — render with a neutral fallback so the
            # data still shows up rather than disappearing.
            meta = {
                "name": slug.replace("_", " ").title(),
                "icon_text": slug[:2].upper(),
                "icon_bg": "bg-zinc-100",
                "icon_text_color": "text-zinc-700",
                "bar_class": "bg-zinc-400",
            }
        share_pct = round(sent * 100 / total_sent) if total_sent > 0 else 0
        out.append(
            {
                "slug": slug,
                "sent": sent,
                "share_pct": share_pct,
                **meta,
            }
        )

    # Sort by share descending (busiest platform first). При равных
    # процентах — по объёму ``sent`` и имени для детерминированного
    # порядка.
    out.sort(key=lambda p: (-p["share_pct"], -p["sent"], p["name"]))
    return out

_PLATFORM_STATUS_LABELS = {
    "active": "Активно",
    "expired": "Сессия протухла",
    "captcha_required": "Ожидает код",
    "error": "Ошибка",
    "disabled": "Не подключено",
}


# Каталог площадок переехал в единый реестр (см.
# ``app/services/job_sites/registry.py``). Шаблон ``settings.html``
# рендерит карточки из ``platforms_list``, который собирается ниже
# в :func:`_platform_state` поверх этого каталога.
from app.services.job_sites.registry import PLATFORMS_CATALOG  # noqa: E402


def _today_ru() -> str:
    """Return today's date as ``"24 Октября 2026"`` in Russian."""
    months = [
        "Января", "Февраля", "Марта", "Апреля", "Мая", "Июня",
        "Июля", "Августа", "Сентября", "Октября", "Ноября", "Декабря",
    ]
    now = datetime.now(timezone.utc)
    return f"{now.day} {months[now.month - 1]} {now.year}"


def _human_full_name(session_user: dict[str, Any]) -> str:
    parts = [session_user.get("first_name") or "", session_user.get("last_name") or ""]
    return " ".join(p for p in parts if p).strip()


def _initials(session_user: dict[str, Any]) -> str:
    first = (session_user.get("first_name") or "").strip()
    last = (session_user.get("last_name") or "").strip()
    return ((first[:1] or "") + (last[:1] or "")).upper() or "?"


async def _user_payload_with_prefs(
    db: AsyncSession,
    user: User,
    session_user: dict[str, Any],
) -> dict[str, Any]:
    """Подмерджить актуальные ``UserPreferences`` поверх ``session_user``.

    Кука ``session["user"]`` пишется один раз в ``/auth/verify-code``
    с пустыми ``first_name``/``last_name`` (preferences к этому моменту
    ещё пустые). Дальше имя/фамилия появляются в БД двумя путями:

    * фоновый ``pull_and_save_resume_for_user`` →
      ``hydrate_user_prefs_from_resume`` → ``UserPreferences.first_name``
      / ``last_name``;
    * юзер сам правит их в форме ``/settings``.

    Кука после этого НЕ обновляется (она подписанная — обновить
    можно только в самом эндпоинте). Поэтому в шаблонах
    ``/dashboard``, ``/responses``, ``/chats``, ``/tariffs``, где
    рендерится профиль-карточка в сайдбаре, имя проявлялось пустым:
    «привет, !». В ``/settings`` всё работало — там был свой
    инлайн-merge.

    Этот хелпер централизует merge: все loader'ы дёргают его и кладут
    результат как ``user`` в template-context. Так нижний-левый
    профиль-блок ведёт себя одинаково на всех страницах.
    """
    prefs = await get_or_create_preferences(db, user)

    first_name = (prefs.first_name or session_user.get("first_name") or "").strip()
    last_name = (prefs.last_name or session_user.get("last_name") or "").strip()

    # ``contact_email`` пустует, пока юзер не введёт его в форме;
    # ``users.email`` синтетика типа "8XXXXXXXXXX@hh.local" — её
    # светить в UI смысла нет.
    if prefs.contact_email:
        email = prefs.contact_email
    elif user.email and not user.email.endswith("@hh.local"):
        email = user.email
    else:
        email = session_user.get("email") or ""

    phone = prefs.contact_phone or session_user.get("phone") or ""

    return {
        **session_user,
        "first_name": first_name,
        "last_name": last_name,
        "email": email,
        "phone": phone,
    }


def _platform_state(
    prefs: PrefsView,
    credentials: list[PlatformCredential],
) -> list[dict[str, Any]]:
    """Merge :data:`PLATFORMS_CATALOG` with live credential rows.

    The returned list is what the settings template iterates over to
    render the platform cards. Each card knows its slug, status, label
    and whether it currently has a pending OTP code (so the inline
    "введите код" form should be expanded on first paint).
    """
    cred_by_slug = {c.platform: c for c in credentials}
    pending = prefs.pending_platform_otps or {}
    out: list[dict[str, Any]] = []
    for entry in PLATFORMS_CATALOG:
        cred = cred_by_slug.get(entry["slug"])
        status = cred.status if cred is not None else "disabled"
        out.append(
            {
                **entry,
                "status": status,
                "status_label": _PLATFORM_STATUS_LABELS.get(status, status),
                "has_pending_code": entry["slug"] in pending,
            }
        )
    return out


# ── /dashboard ──────────────────────────────────────────────────────────────
async def load_dashboard(
    db: AsyncSession,
    user: User,
    session_user: dict[str, Any],
) -> dict[str, Any]:
    """Context for ``dashboard.html``.

    Карточки сверху страницы ``/dashboard``:

    * **Откликов сегодня** — ``stats.applications_today`` /
      ``stats.daily_limit`` (см. :func:`_quota_context`). Источник —
      ``vacancy_responses``, см. ``_quota_context``.
    * **В ожидании** — ``stats.pending``: число чатов, в которых
      работодатель ещё ни разу не отвечал. Совпадает с бейджем
      фильтра «В ожидании» на странице /chats.
    * **Заинтересованы** — ``stats.interested``: число чатов, в
      которых работодатель ответил по-доброму (не отказ).
      Совпадает с бейджем «Заинтересованы» в /chats — это и есть
      «полученные офферы / приглашения», поэтому карточка с
      акцентом-успехом теперь показывает именно их.
    * **Последние отклики** — ``recent_responses``: пять последних
      строк из ``vacancy_responses``.
    """
    prefs = await get_or_create_preferences(db, user)
    quota = await _quota_context(db, user)
    weekly = await _weekly_responses(db, user)
    platform_stats = await _platform_response_stats(db, user)
    chat_counts = await chats_status_counts(db, user_id=user.id)
    recent_response_rows = (
        await db.execute(
            select(VacancyResponse)
            .where(VacancyResponse.user_id == user.id)
            .order_by(VacancyResponse.created_at.desc())
            .limit(5)
        )
    ).scalars().all()
    recent_responses = [_build_response_card(r) for r in recent_response_rows]
    return {
        "user": await _user_payload_with_prefs(db, user, session_user),
        "today": _today_ru(),
        "stats": {
            "applications_today": quota["responses_used_today"],
            "daily_limit": quota["responses_daily_limit"] or 0,
            "weekly_total": weekly["weekly_responses"]["total"],
            "weekly_avg": weekly["weekly_responses"]["avg_per_day"],
            "pending": chat_counts["pending"],
            "interested": chat_counts["interested"],
        },
        "platform_stats": platform_stats,
        # Keep legacy ``platforms`` shape for any leftover bindings; it's
        # no longer used by the new card but harmless.
        "platforms": {"hh": {}, "linkedin": {}, "upwork": {}, "otta": {}},
        "recent_responses": recent_responses,
        "auto_apply_running": prefs.auto_apply_running,
        "chat_counts": chat_counts,
        **quota,
        **weekly,
    }


# ── /responses ──────────────────────────────────────────────────────────────

# Маппинг ``service`` (slug площадки из ``vacancy_responses.service``)
# в короткий бейдж + цвета. Каждой площадке — свой
# фоновый и текстовый цвет (согласовано с _PLATFORM_DISPLAY выше,
# но оставлены рядом для безопасных правок). Добавляешь
# новую площадку — пополняешь эту карту.
_VACANCY_RESPONSE_PLATFORMS: dict[str, dict[str, str]] = {
    "hh":            {"label": "HH",       "css": "bg-red-50 text-red-600"},
    "habr":          {"label": "HABR",     "css": "bg-cyan-50 text-cyan-700"},
    "superjob":      {"label": "SJ",       "css": "bg-emerald-50 text-emerald-700"},
    "linkedin":      {"label": "LI",       "css": "bg-blue-50 text-blue-700"},
    "upwork":        {"label": "UPWORK",   "css": "bg-emerald-50 text-emerald-700"},
    "otta":          {"label": "OTTA",     "css": "bg-zinc-100 text-zinc-700"},
    "github_jobs":   {"label": "GITHUB",   "css": "bg-zinc-900 text-white"},
    "telegram_jobs": {"label": "TG",       "css": "bg-sky-50 text-sky-700"},
    "moi_krug":      {"label": "MK",       "css": "bg-violet-50 text-violet-700"},
    "indeed":        {"label": "INDEED",   "css": "bg-indigo-50 text-indigo-700"},
}
_PLATFORM_DEFAULT_CSS: str = "bg-zinc-100 text-zinc-600"

# Карта статусов карточки отклика. Считаем по ``vacancy_responses``:
#   * ``error`` != NULL          → "Ошибка"     (красный)
#   * ``is_sent`` == True        → "Отправлено" (зелёный)
#   * иначе                      → "В обработке" (янтарный, скрыт в UI)
# Ключ ``filter`` совпадает с ``data-filter`` на кнопках
# шапки в ``responses.html`` ("sent" / "rejected" / "all").
# Статус ``pending`` существует в БД, но в шапке/фильтрах его нет —
# карточки с ним попадают только в «Все отклики».
_RESPONSE_STATUS_STYLES: dict[str, dict[str, str]] = {
    "sent":     {"label": "Отправлено", "css": "bg-emerald-50 text-[#16a34a]", "filter": "sent"},
    "pending":  {"label": "В обработке", "css": "bg-amber-50 text-amber-600",   "filter": "pending"},
    "failed":   {"label": "Ошибка",     "css": "bg-red-50 text-red-500",       "filter": "rejected"},
}


def _response_status_key(row: VacancyResponse) -> str:
    if row.error:
        return "failed"
    if row.is_sent:
        return "sent"
    return "pending"


def _response_company_initials(title: str | None) -> str:
    """Двухсимвольная «аватарка» для левой колонки карточки отклика.

    Берём первые буквы первых двух слов из ``title``; если ``title``
    пустой — возвращаем ``"?"`` (никогда не падаем на проде).
    """
    if not title:
        return "?"
    words = [w for w in title.strip().split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper() or "?"
    return (words[0][:1] + words[1][:1]).upper()


def _to_msk(dt: datetime | None) -> datetime | None:
    """UTC/aware → Europe/Moscow. ``None`` → ``None``.

    ``timestamptz`` из Postgres всегда aware (UTC). Если по какой-то
    причине пришёл naive — считаем его UTC (consistent с тем, как мы
    пишем `_utcnow`).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_MSK)


def _iso(dt: datetime | None) -> str:
    """ISO-8601 строка для ``data-iso`` атрибута (JS перерендерит в локаль).

    Возвращаем в UTC, чтобы у JS не было неоднозначности с TZ.
    """
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _format_response_date(dt: datetime | None) -> str:
    """Дата карточки отклика в виде ``dd.MM HH:MM`` по Москве.

    Год не показываем — и без него колонка была узковата, отклики
    приходят в пределах текущего года — в большинстве случаев
    этого достаточно.
    """
    dt = _to_msk(dt)
    if dt is None:
        return ""
    return dt.strftime("%d.%m %H:%M")


def _build_response_card(row: VacancyResponse) -> dict[str, Any]:
    status_key = _response_status_key(row)
    style = _RESPONSE_STATUS_STYLES[status_key]
    platform_meta = _VACANCY_RESPONSE_PLATFORMS.get(row.service)
    if platform_meta:
        platform_label = platform_meta["label"]
        platform_css = platform_meta["css"]
    else:
        platform_label = (row.service or "—").upper()
        platform_css = _PLATFORM_DEFAULT_CSS
    # Разделяем «название вакансии» и «имя компании»:
    # в карточке большим чёрным показываем название вакансии
    # (``title``), мелким серым — компанию (``company``). Старые
    # строки без ``company`` имели в ``title`` имя компании —
    # там второй ряд просто не рисуется.
    title_text = (row.title or "").strip() or "—"
    company_text = (row.company or "").strip()
    # Инициалы берём из имени компании, если оно есть —
    # иначе из названия вакансии (старые строки в БД).
    logo_ext = getattr(row, "logo_ext", None)
    logo_url = (
        f"/static/logotips/{row.id}.{logo_ext}" if logo_ext else None
    )
    return {
        "id": str(row.id),
        "logo_url": logo_url,
        "company_initials": _response_company_initials(company_text or title_text),
        "title": title_text,
        "company": company_text,
        "salary": row.salary or None,
        "platform": platform_label,
        "platform_css": platform_css,
        "status": style["label"],
        "status_css": style["css"],
        "status_filter": style["filter"],
        "link": row.link or "",
        "created_at": _format_response_date(row.created_at),
        # ISO в UTC — фронт перерендерит в локальную TZ браузера, если
        # она отличается от Москвы.
        "created_at_iso": _iso(row.created_at),
    }


async def get_response_cards_and_stats(
    db: AsyncSession,
    user: User,
) -> dict[str, Any]:
    """Compact payload для live-обновления /responses без перезагрузки.

    Используется одновременно `load_responses` (SSR) и
    `GET /api/responses` (polling из браузера каждые ~20 сек).
    """
    return {
        "responses": await _vacancy_response_cards(db, user),
        "response_stats": await _vacancy_response_stats(db, user),
    }


async def _vacancy_response_cards(
    db: AsyncSession,
    user: User,
) -> list[dict[str, Any]]:
    """Все строки ``vacancy_responses`` юзера, отсортированные сверху
    вниз по ``created_at DESC`` и завёрнутые в dict под шаблон
    ``responses.html``."""
    rows = (
        await db.execute(
            select(VacancyResponse)
            .where(VacancyResponse.user_id == user.id)
            .order_by(VacancyResponse.created_at.desc())
        )
    ).scalars().all()
    return [_build_response_card(r) for r in rows]


async def _vacancy_response_stats(
    db: AsyncSession,
    user: User,
) -> dict[str, int]:
    """Агрегаты для четырёх карточек в шапке ``/responses``.

    * ``total``    — всего (для кнопки «Все отклики»);
    * ``sent``     — успешно отправленные (``is_sent=true``);
    * ``pending``  — в обработке (ещё не дошли до отправки);
    * ``rejected`` — упали с ошибкой (``error`` != NULL).
    """
    rows = (
        await db.execute(
            select(VacancyResponse.is_sent, VacancyResponse.error).where(
                VacancyResponse.user_id == user.id,
            )
        )
    ).all()
    sent = 0
    pending = 0
    rejected = 0
    for is_sent, error in rows:
        if error:
            rejected += 1
        elif is_sent:
            sent += 1
        else:
            pending += 1
    return {
        "total": sent + pending + rejected,
        "sent": sent,
        "pending": pending,
        "rejected": rejected,
    }


async def load_responses(
    db: AsyncSession,
    user: User,
    session_user: dict[str, Any],
) -> dict[str, Any]:
    """Context for ``responses.html``."""
    prefs = await get_or_create_preferences(db, user)
    quota = await _quota_context(db, user)
    responses = await _vacancy_response_cards(db, user)
    response_stats = await _vacancy_response_stats(db, user)
    return {
        "user": await _user_payload_with_prefs(db, user, session_user),
        "today": _today_ru(),
        "response_stats": response_stats,
        "responses": responses,
        "auto_apply_running": prefs.auto_apply_running,
        **quota,
    }


# ── /chats ──────────────────────────────────────────────────────────────────
def _initials_from_title(title: str | None) -> str:
    if not title:
        return "—"
    parts = [p for p in title.split() if p]
    if not parts:
        return "—"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[1][:1]).upper()


def _format_chat_time(dt: datetime | None) -> str:
    """Короткое время для списка чатов — в Europe/Moscow.

    Сегодня — ``HH:MM``, в этом году — ``DD.MM``, другой год — ``DD.MM.YY``.
    """
    dt = _to_msk(dt)
    if dt is None:
        return ""
    now = datetime.now(_MSK)
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if dt.year == now.year:
        return dt.strftime("%d.%m")
    return dt.strftime("%d.%m.%y")


def _format_message_time(dt: datetime | None) -> str:
    dt = _to_msk(dt)
    if dt is None:
        return ""
    return dt.strftime("%H:%M")


# ── Распознавание «оффера» в тексте ───────────────────────────────────
#
# Типовые фразы заготовленных «оффер-писем» / финальных предложений.
# Все ключевые подстроки — короткие, в нижнем регистре, без знаков
# препинания, чтобы матчиться независимо от того, как HR собрал фразу.
# Если нужно добавить шаблон — клади сюда же.
_OFFER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE)
    for p in (
        r"\bоффер[ауеыов]?\b",
        r"\bofferd?\b",
        r"готовы?\s+(?:сделать|предложить)\s+(?:вам\s+)?оффер",
        r"высыла[ею]м\s+(?:вам\s+)?оффер",
        r"направля[ею]м\s+(?:вам\s+)?оффер",
        r"присыла[ею]м\s+(?:вам\s+)?оффер",
        r"предлага[ею]м\s+(?:вам\s+)?(?:позици[июя]|должность|работу|выйти|оффер)",
        r"приглаша[ею]м\s+(?:вас\s+)?на\s+работу",
    )
)


def is_offer_text(text: str | None) -> bool:
    """Содержит ли текст шаблонные фразы «оффер»."""
    if not text:
        return False
    norm = text.replace("\xa0", " ")
    for pat in _OFFER_PATTERNS:
        if pat.search(norm):
            return True
    return False


def _sanitize_preview(service: str | None, text: str | None) -> str:
    """Убрать HTML-теги из превью/тела habr-сообщений.

    Старые строки в БД были записаны до фикса в ``_build_chat_row`` —
    в них всё ещё лежат буквальные ``<p>`` / ``<a href>`` / ``<br>``.
    Применяем ``_html_to_plain`` на чтении: для plain-text это no-op
    (нет ``<``), для legacy-данных — нормализуем.
    """
    if not text:
        return ""
    if service == "habr" and "<" in text and ">" in text:
        return _habr_html_to_plain(text)
    return text


async def list_chat_cards(
    db: AsyncSession,
    *,
    user_id: Any,
) -> list[dict[str, Any]]:
    """Сериализация всех чатов юзера в плоский JSON-список карточек.

    Используется и при SSR (``load_chats``), и при опросе
    ``/api/chats/sync``, чтобы клиент после фонового sync'а мог
    перерисовать список без полного reload страницы. Без ``is_active``
    — этот флаг считает уже фронт по текущему URL.
    """
    from app.services.job_sites.hh.chats_sync import (
        classify_chat_status,
        is_chat_relevant,
    )

    chat_rows = (
        await db.execute(
            select(Chat)
            .where(Chat.user_id == user_id)
            .order_by(
                Chat.last_activity_at.desc().nullslast(),
                Chat.created_at.desc(),
            )
        )
    ).scalars().all()

    # Подгружаем самое раннее входящее сообщение по каждому чату — оно
    # определяет статус («Отказ» / «Заинтересованы»). Раньше статус
    # считали только по последнему сообщению, и любой наш ответ возвращал
    # чат в «В ожидании», даже когда диалог давно начался.
    first_incoming_by_chat: dict[Any, str] = {}
    if chat_rows:
        chat_ids = [c.id for c in chat_rows]
        rn_col = (
            func.row_number()
            .over(
                partition_by=Message.chat_id,
                order_by=(Message.sent_at.asc(), Message.id.asc()),
            )
            .label("rn")
        )
        first_incoming_subq = (
            select(Message.chat_id, Message.text, rn_col)
            .where(
                Message.chat_id.in_(chat_ids),
                Message.author == "them",
            )
            .subquery()
        )
        rows = (
            await db.execute(
                select(
                    first_incoming_subq.c.chat_id,
                    first_incoming_subq.c.text,
                ).where(first_incoming_subq.c.rn == 1)
            )
        ).all()
        for chat_id, text in rows:
            first_incoming_by_chat[chat_id] = text or ""

    cards: list[dict[str, Any]] = []
    for c in chat_rows:
        relevant = is_chat_relevant(
            applicant_state=c.applicant_state,
            last_message_outgoing=c.last_message_outgoing,
        )
        first_incoming_text = first_incoming_by_chat.get(c.id)
        last_preview = _sanitize_preview(c.service, c.last_message_preview)
        status = classify_chat_status(
            applicant_state=c.applicant_state,
            last_message_outgoing=c.last_message_outgoing,
            last_message_text=last_preview,
            first_incoming_text=first_incoming_text,
            has_incoming_message=c.id in first_incoming_by_chat,
        )
        cards.append(
            {
                "id": str(c.id),
                "href": f"/chats?id={c.id}",
                "link": c.link,
                "title": c.title,
                "subtitle": c.subtitle or "",
                "initials": _initials_from_title(c.subtitle or c.title),
                "last_message_at": _format_chat_time(c.last_activity_at),
                "last_message_at_iso": _iso(c.last_activity_at),
                # Последнее сообщение в любом случае показываем отдельной
                # строкой — статус идёт отдельным бейджом под названием компании.
                "last_message_preview": last_preview,
                "status_kind": status["kind"],
                "status_label": status["label"],
                "status_css": status["css"],
                # Алиасы для обратной совместимости со старыми шаблонами/фронтом.
                "preview_kind": status["kind"],
                "preview_label": status["label"],
                "preview_css": status["css"],
                "unread_messages": c.unread_messages,
                "is_relevant": relevant,
            }
        )
    return cards


async def chats_status_counts(
    db: AsyncSession,
    *,
    user_id: Any,
) -> dict[str, int]:
    """Подсчитать количество чатов по статусам — для бейджей фильтров.

    Возвращает dict с ключами:
      * ``all``        — всего чатов;
      * ``relevant``   — «актуальные» (последнее сообщение от
        работодателя И не DISCARD), см. :func:`is_chat_relevant`;
      * ``pending``    — «в ожидании» (работодатель ни разу не ответил);
      * ``refusal``    — «отказы» (см. :func:`classify_chat_status`);
      * ``interested`` — «заинтересованы»;
      * ``offer``      — есть оффер в одном из сообщений
        (по шаблонам :func:`is_offer_text`).

    Один проход по ``chats`` + один по ``messages`` (для самого
    раннего входящего по каждому чату) + один сводный COUNT по
    наличию оффера в любом сообщении чата. Никаких N+1.
    """
    from app.services.job_sites.hh.chats_sync import (
        classify_chat_status,
        is_chat_relevant,
    )

    chat_rows = (
        await db.execute(
            select(Chat).where(Chat.user_id == user_id)
        )
    ).scalars().all()
    counts = {
        "all": 0,
        "relevant": 0,
        "pending": 0,
        "refusal": 0,
        "interested": 0,
        "offer": 0,
    }
    if not chat_rows:
        return counts

    chat_ids = [c.id for c in chat_rows]

    # Самое раннее входящее сообщение для каждого чата — нужно для
    # определения статуса «Отказ» / «Заинтересованы».
    rn_col = (
        func.row_number()
        .over(
            partition_by=Message.chat_id,
            order_by=(Message.sent_at.asc(), Message.id.asc()),
        )
        .label("rn")
    )
    first_incoming_subq = (
        select(Message.chat_id, Message.text, rn_col)
        .where(
            Message.chat_id.in_(chat_ids),
            Message.author == "them",
        )
        .subquery()
    )
    first_incoming_rows = (
        await db.execute(
            select(
                first_incoming_subq.c.chat_id,
                first_incoming_subq.c.text,
            ).where(first_incoming_subq.c.rn == 1)
        )
    ).all()
    first_incoming_by_chat: dict[Any, str] = {
        chat_id: text or "" for chat_id, text in first_incoming_rows
    }

    # Чаты, в которых есть хотя бы одно входящее сообщение с признаками
    # оффера. Один SQL по messages.
    incoming_texts = (
        await db.execute(
            select(Message.chat_id, Message.text).where(
                Message.chat_id.in_(chat_ids),
                Message.author == "them",
            )
        )
    ).all()
    offer_chat_ids: set[Any] = set()
    for chat_id, text in incoming_texts:
        if chat_id in offer_chat_ids:
            continue
        if is_offer_text(text):
            offer_chat_ids.add(chat_id)

    for c in chat_rows:
        counts["all"] += 1
        if is_chat_relevant(
            applicant_state=c.applicant_state,
            last_message_outgoing=c.last_message_outgoing,
        ):
            counts["relevant"] += 1
        last_preview = _sanitize_preview(c.service, c.last_message_preview)
        first_incoming_text = first_incoming_by_chat.get(c.id)
        status = classify_chat_status(
            applicant_state=c.applicant_state,
            last_message_outgoing=c.last_message_outgoing,
            last_message_text=last_preview,
            first_incoming_text=first_incoming_text,
            has_incoming_message=c.id in first_incoming_by_chat,
        )
        kind = status["kind"]
        if kind == "pending":
            counts["pending"] += 1
        elif kind == "refusal":
            counts["refusal"] += 1
        elif kind == "interested":
            counts["interested"] += 1
        if c.id in offer_chat_ids:
            counts["offer"] += 1
    return counts


async def _mark_chat_read_inline(
    db: AsyncSession,
    *,
    user_id: Any,
    chat_uuid: Any,
) -> None:
    """Локально пометить чат прочитанным внутри текущей транзакции.

    Что делает:
      * сбрасывает ``Chat.unread_messages = 0``;
      * двигает ``Chat.last_viewed_message_id`` на самый свежий
        ``Message.external_id`` этого чата (чтобы ближайший sync с hh.ru,
        где наш mark_read мог ещё не пропечататься, не воскресил
        счётчик — см. ``_upsert_chat_and_last_message``);
      * помечает все ``Message.is_read = True`` для этого чата.

    Идемпотентно: если уже всё прочитано, лишних UPDATE не возникает.
    Хождение на hh.ru здесь НЕ делаем — это синхронный путь рендера
    страницы, дёргать внешнюю сетку нельзя. hh-сторона помечается из
    клиентского ``markReadOnClick`` (см. ``app/templates/chats.html``).
    """
    chat_row = (
        await db.execute(
            select(Chat).where(Chat.id == chat_uuid, Chat.user_id == user_id)
        )
    ).scalar_one_or_none()
    if chat_row is None:
        return

    latest_external = (
        await db.execute(
            select(Message.external_id)
            .where(
                Message.chat_id == chat_row.id,
                Message.external_id.is_not(None),
            )
            .order_by(Message.sent_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    changed = False
    if chat_row.unread_messages != 0:
        chat_row.unread_messages = 0
        changed = True
    if latest_external is not None and chat_row.last_viewed_message_id != latest_external:
        # Не «откатываем» last_viewed назад — только вперёд.
        try:
            cur = int(chat_row.last_viewed_message_id) if chat_row.last_viewed_message_id else None
            new = int(latest_external)
        except (TypeError, ValueError):
            cur, new = None, None
        if cur is None or (new is not None and new > cur):
            chat_row.last_viewed_message_id = latest_external
            changed = True

    # Сообщения помечаем во всех случаях — это `UPDATE ... WHERE is_read=False`,
    # без работы если делать нечего.
    await db.execute(
        Message.__table__.update()
        .where(Message.chat_id == chat_row.id, Message.is_read.is_(False))
        .values(is_read=True)
    )

    if changed:
        await db.flush()


async def build_active_chat_ctx(
    db: AsyncSession,
    *,
    user: User,
    chat: Chat,
    hh_phone: str | None,
    habr_email: str | None = None,
    fetch_remote: bool = False,
) -> dict[str, Any]:
    """Собрать контекст активного диалога: шапка + все сообщения.

    Дефолт — ``fetch_remote=False``: контекст собирается чисто из БД,
    что укладывается в ~50ms (vs ~1-2s c походом на удалённый API).
    Фронт отдельно дёргает ``POST /api/chats/<id>/refresh``, чтобы
    догрузить свежие сообщения и обновить DOM по факту. Так нет
    «подвисания» при клике на чат, а данные подтягиваются прозрачно.

    Если ``fetch_remote=True`` и есть креды для площадки чата —
    синхронно тянем свежую историю и апсёртим её в локальный
    ``messages``. Сетевой провал не валит рендер.

    Маршрут «куда идти за свежими сообщениями» определяется
    ``chat.service``:

    * ``hh``   — ``chatik.hh.ru/chatik/api/chat_data`` (нужен
      ``hh_phone`` из cookie-сессии / ``user.phone_number``);
    * ``habr`` — ``career.habr.com/api/frontend_v1/chat/messages`` (нужен
      ``habr_email``; cookies лежат в
      ``platform_credentials.encrypted_session`` по ``user_id``).
    """
    if fetch_remote and chat.service == "hh" and hh_phone:
        from app.services.job_sites.hh.chats_sync import (
            sync_hh_chat_messages_for_user,
        )

        try:
            await sync_hh_chat_messages_for_user(
                db, user_id=user.id, chat=chat, hh_phone=hh_phone,
            )
        except Exception:
            # Логирование — внутри sync_hh_chat_messages_for_user; здесь
            # просто не валим рендер страницы.
            pass
    elif fetch_remote and chat.service == "habr":
        from app.services.job_sites.habr.chats_sync import (
            sync_habr_chat_messages_for_user,
        )

        try:
            await sync_habr_chat_messages_for_user(
                db, user_id=user.id, chat=chat,
                habr_email=habr_email or "",
            )
        except Exception:
            # Логирование — внутри sync_habr_chat_messages_for_user.
            pass

    messages_rows = (
        await db.execute(
            select(Message)
            .where(Message.chat_id == chat.id)
            .order_by(Message.sent_at.asc())
        )
    ).scalars().all()
    messages_ctx: list[dict[str, Any]] = []
    for m in messages_rows:
        outgoing = m.author == "me"
        author_name = m.author_name or (
            "Я" if outgoing else (chat.subtitle or "HR")
        )
        # Старые habr-сообщения в БД могут содержать буквальные HTML-теги
        # (``<p>``, ``<a href>``, ``<br>``) — они уходили на фронт и
        # отображались как текст. На чтении нормализуем до plain-text.
        text = _sanitize_preview(chat.service, m.text)
        messages_ctx.append(
            {
                "outgoing": outgoing,
                "text": text,
                "sent_at": _format_message_time(m.sent_at),
                "sent_at_iso": _iso(m.sent_at),
                "author_initials": _initials_from_title(author_name),
            }
        )
    return {
        "id": str(chat.id),
        "link": chat.link,
        # Сверху в шапке диалога — название вакансии (``title``),
        # снизу — компания (``subtitle``).
        "contact_name": chat.title,
        "subtitle": chat.subtitle or "",
        "initials": _initials_from_title(chat.subtitle or chat.title),
        "messages": messages_ctx,
    }


async def load_chats(
    db: AsyncSession,
    user: User,
    session_user: dict[str, Any],
    *,
    active_chat_id: str | None = None,
) -> dict[str, Any]:
    """Context for ``chats.html``.

    Читает все чаты юзера из БД, отсортированные по времени последней
    активности. Если передан ``active_chat_id`` — дополнительно
    подгружает сообщения этого диалога И сразу сбрасывает его в БД в
    «прочитан» (``unread_messages=0``, все ``messages.is_read=True``,
    ``last_viewed_message_id`` — на самое свежее сообщение). Это
    делается **до** рендера, чтобы зелёный бейдж непрочитанных пропал
    мгновенно, без ожидания фонового sync'а; и одновременно фиксирует
    «локально просмотрено», чтобы ближайший sync с hh.ru (где
    mark_read мог ещё не дойти) не воскресил счётчик — см.
    ``_upsert_chat_and_last_message`` в ``chats_sync``.

    Синхронизацию с hh.ru (``chatik.hh.ru/chatik/api/chats``) здесь НЕ
    дёргаем — это долгий сетевой вызов, он живёт отдельным эндпойнтом
    (``POST /api/chats/sync``), который фронт дёргает по лоаду страницы.
    Так первый рендер всегда остаётся быстрым (просто SELECT из БД),
    а свежие данные подтягиваются в фоне.
    """
    quota = await _quota_context(db, user)

    active_chat_uuid = None
    if active_chat_id:
        from uuid import UUID

        try:
            active_chat_uuid = UUID(active_chat_id)
        except (ValueError, AttributeError):
            active_chat_uuid = None

    # Если открыт конкретный диалог — мгновенно гасим непрочитанные.
    # Делаем это ДО выборки списка чатов, чтобы SELECT уже видел
    # новое значение ``unread_messages=0`` (одна транзакция).
    if active_chat_uuid is not None:
        await _mark_chat_read_inline(
            db, user_id=user.id, chat_uuid=active_chat_uuid
        )

    # Карточки списка — общий хелпер, тот же набор полей возвращает
    # фоновый ``POST /api/chats/sync`` для клиентской перерисовки.
    chat_cards = await list_chat_cards(db, user_id=user.id)

    chats_ctx: list[dict[str, Any]] = []
    active_chat_uuid_str = (
        str(active_chat_uuid) if active_chat_uuid is not None else None
    )
    for card in chat_cards:
        ctx = dict(card)
        ctx["is_active"] = (
            active_chat_uuid_str is not None and card["id"] == active_chat_uuid_str
        )
        chats_ctx.append(ctx)

    active_chat_ctx: dict[str, Any] | None = None
    if active_chat_uuid is not None:
        active_chat = (
            await db.execute(
                select(Chat).where(
                    Chat.id == active_chat_uuid, Chat.user_id == user.id
                )
            )
        ).scalar_one_or_none()
        if active_chat is not None:
            # SSR-рендер — без блокирующего похода на hh.ru за полной
            # историей: фронт сам дёрнет ``POST /api/chats/<id>/refresh``
            # в фоне и обновит DOM, если придут свежие сообщения.
            active_chat_ctx = await build_active_chat_ctx(
                db, user=user, chat=active_chat, hh_phone=None,
            )

    chat_counts = await chats_status_counts(db, user_id=user.id)

    return {
        "user": await _user_payload_with_prefs(db, user, session_user),
        "today": _today_ru(),
        "chats": chats_ctx,
        "active_chat": active_chat_ctx,
        "chat_counts": chat_counts,
        **quota,
    }


# ── /settings ───────────────────────────────────────────────────────────────
async def load_settings(
    db: AsyncSession,
    user: User,
    session_user: dict[str, Any],
) -> dict[str, Any]:
    """Context for ``settings.html`` — backed by real DB rows."""

    # Бесшовно дозаливаем temp_email / platform_password для старых
    # юзеров (создались до выкатки фичи). Идемпотентно — если поля
    # уже заполнены, ничего не делает.
    await ensure_platform_credentials(db, user)

    prefs = await get_or_create_preferences(db, user)

    # Resume: at most one per user (model declares user_id as unique).
    resume_row = (
        await db.execute(select(Resume).where(Resume.user_id == user.id))
    ).scalar_one_or_none()
    resume_ctx: dict[str, Any] | None = None
    if resume_row is not None:
        parsed = resume_row.parsed_data or {}
        skills_raw = parsed.get("skills")
        skills: list[str] = list(skills_raw) if isinstance(skills_raw, list) else []
        summary_parts: list[str] = []
        if skills:
            summary_parts.append(", ".join(skills[:6]))
        years = parsed.get("years_of_experience")
        if isinstance(years, (int, float)) and years > 0:
            summary_parts.append(f"{int(years)} лет опыта")
        title_val = parsed.get("title")
        resume_ctx = {
            "title": title_val if isinstance(title_val, str) else None,
            "summary": " · ".join(summary_parts) if summary_parts else None,
        }

    credentials_rows = (
        await db.execute(
            select(PlatformCredential).where(PlatformCredential.user_id == user.id)
        )
    ).scalars().all()

    platforms_list = _platform_state(prefs, list(credentials_rows))
    pc_active = sum(1 for p in platforms_list if p["status"] == "active")
    pc_errors = sum(
        1 for p in platforms_list if p["status"] in ("error", "expired", "captcha_required")
    )
    pc_disconnected = sum(1 for p in platforms_list if p["status"] == "disabled")

    # Шаблоны сопроводительных больше не храним — LLM генерирует их
    # на лету по резюме и описанию вакансии. Для фронтовог
    # textarea отдаём пустую строку.
    default_cover_letter = ""

    # Compose the preferences dict the template expects.  We expose
    # individual fields as well as a `prefs` map so existing
    # ``{{ prefs.position }}``-style bindings keep working.
    work_formats: list[str] = list(prefs.work_formats or [])
    prefs_ctx = {
        "position": prefs.position or "",
        "grade": prefs.grade or "",
        "stack": prefs.stack or "",
        "salary": prefs.salary or "",
        "work_formats": work_formats,
        "stop_companies": prefs.stop_companies or "",
        "stop_words": prefs.stop_words or "",
        "profile_url": prefs.profile_url or "",
        "language": prefs.language or "Русский",
        "currency": prefs.currency or "RUB ₽",
        "timezone": prefs.timezone_name or "Москва, GMT+3",
        "ai_cover_letter_enabled": prefs.ai_cover_letter_enabled,
    }

    notifications_ctx = {
        "email": prefs.notif_email,
        "push": prefs.notif_push,
        "messages": prefs.notif_messages,
        "weekly": prefs.notif_weekly,
        "telegram": prefs.notif_telegram,
    }

    # Имя/фамилия/email/телефон строим через общий хелпер — он же
    # используется на /dashboard, /responses, /chats, /tariffs. Так
    # сайдбар-карточка профиля одинаковая везде.
    user_payload = await _user_payload_with_prefs(db, user, session_user)
    user_payload["two_factor_enabled"] = prefs.two_factor_enabled

    full_name = " ".join(
        p for p in [user_payload["first_name"], user_payload["last_name"]] if p
    ).strip()
    initials = (
        ((user_payload["first_name"][:1] or "") + (user_payload["last_name"][:1] or ""))
        .upper()
        or "?"
    )

    quota = await _quota_context(db, user)

    # «Доступы для ручного входа» — temp_email из temp.coda.ink и
    # детерминированный platform_password. Передаём в контекст
    # компактным словарём, чтобы шаблон не дёргал ``user.*`` напрямую.
    platform_access = {
        "temp_email": user.temp_email or "",
        "password": user.platform_password or "",
    }

    return {
        "user": user_payload,
        "today": _today_ru(),
        "user_full_name": full_name,
        "user_initials": initials,
        "resume": resume_ctx,
        "prefs": prefs_ctx,
        "platforms_list": platforms_list,
        "platform_credentials_active": pc_active,
        "platform_credentials_errors": pc_errors,
        "platform_credentials_disconnected": pc_disconnected,
        "platform_access": platform_access,
        "default_cover_letter": default_cover_letter,
        "notifications": notifications_ctx,
        "auto_apply_running": prefs.auto_apply_running,
        "telegram_chat_id": user.telegram_chat_id,
        "telegram_link_token": prefs.telegram_link_token or "",
        **quota,
    }


# ── /tariffs ────────────────────────────────────────────────────────────────
PLAN_CATALOG: list[dict[str, Any]] = [
    {
        "slug": "free",
        "name": "Free",
        "eyebrow": "Старт",
        "tagline": "Чтобы попробовать платформу и понять, как работают авто-отклики.",
        "price_rub": 0,
        "rank": 0,
        "features": [
            "До 50 откликов в месяц",
            "Подключение 1 площадки (hh.ru)",
            "Базовые шаблоны сопроводительных",
            "Email-уведомления",
        ],
        "features_off": [
            "AI-генерация сопроводительных писем",
            "Telegram-уведомления",
            "Поддержка 24/7",
        ],
    },
    {
        "slug": "basic",
        "name": "Basic",
        "eyebrow": "Для активного поиска",
        "tagline": "Для тех, кто откликается ежедневно и хочет автоматизировать рутину.",
        "price_rub": 690,
        "rank": 1,
        "is_recommended": True,
        "features": [
            "До 1000 откликов в месяц",
            "Подключение всех площадок",
            "AI-генерация сопроводительных писем",
            "Email + Push-уведомления",
            "Telegram-уведомления",
        ],
        "features_off": [
            "Приоритетная поддержка 24/7",
        ],
    },
    {
        "slug": "pro",
        "name": "Pro",
        "eyebrow": "Полный комплект",
        "tagline": "Без лимитов и с приоритетной поддержкой — для самых требовательных.",
        "price_rub": 1490,
        "rank": 2,
        "features": [
            "Без лимитов на отклики",
            "Подключение всех площадок",
            "AI-генерация сопроводительных писем",
            "Все каналы уведомлений",
            "Приоритетная поддержка 24/7",
            "Расширенная аналитика и отчёты",
        ],
        "features_off": [],
    },
]

_PLAN_LABELS = {"free": "FREE PLAN", "basic": "BASIC PLAN", "pro": "PRO PLAN"}
_STATUS_LABELS = {
    "trial": "Пробный период.",
    "active": "Подписка активна.",
    "expired": "Срок истёк.",
    "cancelled": "Подписка отменена.",
    "paused": "Подписка на паузе.",
}


async def load_tariffs(
    db: AsyncSession,
    user: User,
    session_user: dict[str, Any],
) -> dict[str, Any]:
    """Context for ``tariffs.html``."""
    sub = (
        await db.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one_or_none()
    current_plan = (sub.plan if sub else "free") or "free"
    current_status = (sub.status if sub else "trial") or "trial"
    current_rank = next(
        (p["rank"] for p in PLAN_CATALOG if p["slug"] == current_plan), 0
    )

    plans: list[dict[str, Any]] = []
    for entry in PLAN_CATALOG:
        plans.append(
            {
                **entry,
                "is_current": entry["slug"] == current_plan,
                "is_downgrade": entry["rank"] < current_rank,
                "is_recommended": bool(entry.get("is_recommended", False))
                and entry["slug"] != current_plan,
            }
        )

    quota = await _quota_context(db, user)

    return {
        "user": await _user_payload_with_prefs(db, user, session_user),
        "today": _today_ru(),
        "plans": plans,
        "current_plan_slug": current_plan,
        "current_plan_label": _PLAN_LABELS.get(current_plan, current_plan.upper()),
        "current_status_label": _STATUS_LABELS.get(
            current_status, "Действует базовый тариф."
        ),
        **{k: v for k, v in quota.items() if k != "current_plan_slug"},
    }
