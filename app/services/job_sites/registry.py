"""Реестр поддерживаемых job-площадок.

Это **единственный** источник правды по списку площадок. Везде, где
в проекте появляется `platform: str`, валидное значение должно
браться отсюда:

* На фронте (settings.html) карточки рисуются через
  ``PLATFORMS_CATALOG`` (см. ниже) — никаких хардкоженных списков
  в шаблоне.
* На бэке (``app/api/dashboard.py: settings_platforms_*``)
  ``platform`` из запроса валидируется через :func:`is_supported`.
  Левый slug → 400.
* В воркерах (``WorkerContext.active_platforms``) при загрузке
  отфильтровывается через тот же :func:`is_supported`. Если в БД
  лежит learnt-в-древности slug, которого больше нет в реестре —
  он не попадёт в воркер. Это защищает от того, что воркер пойдёт
  «откликаться на платформе, которой больше нет в коде».

## Как добавить новую площадку

1. Дописать константу `PLATFORM_<NAME>` ниже.
2. Дописать запись в `PLATFORMS_CATALOG` (в нужном порядке —
   именно в этом порядке карточки появятся в UI).
3. Добавить implementation-модуль в ``app/services/job_sites/<slug>/``
   (по образцу ``hh/``). Воркер ``work_at_all`` будет ходить туда
   через diapatcher (когда диспатчер появится).

Slug должен быть строго ``[a-z0-9_]+`` — он сохраняется в
``platform_credentials.platform`` и используется как ключ во всех
JSON-структурах (``pending_platform_otps``, статистики и т.п.).
"""
from __future__ import annotations

from typing import Any


# ── Константы slug'ов ────────────────────────────────────────────────
#
# Используй именно эти константы в коде вместо магических строк
# ("hh", "habr", ...). Тогда при опечатке упадёт сразу импорт, а не
# где-то в рантайме.
PLATFORM_HH = "hh"
PLATFORM_HABR = "habr"
PLATFORM_LINKEDIN = "linkedin"
PLATFORM_GITHUB_JOBS = "github_jobs"
PLATFORM_TELEGRAM_JOBS = "telegram_jobs"
PLATFORM_MOI_KRUG = "moi_krug"
PLATFORM_INDEED = "indeed"
PLATFORM_GLASSDOOR = "glassdoor"


# ── Каталог для рендера UI ───────────────────────────────────────────
#
# Порядок здесь = порядок карточек на /settings → "Подключённые
# площадки". В каждом entry — slug + всё, что нужно шаблону для
# отрисовки (название, иконка, цвет, подзаголовок).
#
# Никаких state'ов (active/error/pending) тут нет — это статический
# каталог. Состояние юзера наслаивается отдельно в
# ``app/db/dashboard_data.py:_platform_state`` из ``PlatformCredential``.
PLATFORMS_CATALOG: list[dict[str, Any]] = [
    {
        "slug": PLATFORM_HH,
        "name": "hh.ru",
        "subtitle": "OAuth",
        "icon_kind": "text",
        "icon_text": "HH",
        "icon_bg": "bg-[#ef4444]",
    },
    {
        "slug": PLATFORM_LINKEDIN,
        "name": "LinkedIn",
        "subtitle": "Cookies / расширение",
        "icon_kind": "icon",
        "icon_class": "ph-linkedin-logo",
        "icon_bg": "bg-blue-600",
    },
    {
        "slug": PLATFORM_GITHUB_JOBS,
        "name": "GitHub Jobs",
        "subtitle": "API токен",
        "icon_kind": "icon",
        "icon_class": "ph-github-logo",
        "icon_bg": "bg-black",
    },
    {
        "slug": PLATFORM_HABR,
        "name": "Habr Career",
        "subtitle": "Cookies",
        "icon_kind": "text",
        "icon_text": "HNT",
        "icon_bg": "bg-[#00b2ff]",
    },
    {
        "slug": PLATFORM_TELEGRAM_JOBS,
        "name": "Telegram Jobs",
        "subtitle": "Bot API",
        "icon_kind": "icon",
        "icon_class": "ph-telegram-logo",
        "icon_bg": "bg-yellow-500",
    },
    {
        "slug": PLATFORM_MOI_KRUG,
        "name": "Moi Krug",
        "subtitle": "Cookies",
        "icon_kind": "text",
        "icon_text": "MO",
        "icon_bg": "bg-zinc-800",
    },
    {
        "slug": PLATFORM_INDEED,
        "name": "Indeed",
        "subtitle": "Cookies",
        "icon_kind": "icon",
        "icon_class": "ph-briefcase",
        "icon_bg": "bg-zinc-300",
    },
    {
        "slug": PLATFORM_GLASSDOOR,
        "name": "Glassdoor",
        "subtitle": "Cookies",
        "icon_kind": "icon",
        "icon_class": "ph-globe",
        "icon_bg": "bg-zinc-300",
    },
]


# ── Производные структуры (для O(1) валидации) ───────────────────────
#
# Производим из ``PLATFORMS_CATALOG`` — чтобы не было риска забыть
# обновить set, добавив новую площадку в каталог.
SUPPORTED_PLATFORM_SLUGS: frozenset[str] = frozenset(
    entry["slug"] for entry in PLATFORMS_CATALOG
)


def is_supported(slug: str | None) -> bool:
    """``True``, если ``slug`` — известная нам площадка.

    Использовать в API-ручках перед записью в БД и в воркерах
    перед попыткой постить отклик.
    """
    return bool(slug) and slug in SUPPORTED_PLATFORM_SLUGS


def filter_supported(slugs: list[str]) -> list[str]:
    """Возвращает только те slug'и из ``slugs``, которые есть в
    реестре. Удобно для ``WorkerContext`` — отбрасывает legacy-данные."""
    return [s for s in slugs if s in SUPPORTED_PLATFORM_SLUGS]
