"""Dispatcher: ходит по площадкам через ``BaseJobSiteClient``-подклассы.

Главные функции наружу:

* :func:`get_client_class` — slug → класс клиента (или ``None``,
  если для этого slug'а ещё нет реализации).
* :func:`active_implementations` — пара ``(slug, cls)`` для всех
  площадок, у которых **есть** и запись в каталоге UI
  (``registry.PLATFORMS_CATALOG``) и зарегистрированный клиент.

Пример работы воркера ``work_at_all``:

.. code-block:: python

    from app.services.job_sites.dispatcher import get_client_class

    async def work_at_all(ctx: WorkerContext) -> dict:
        for slug in ctx.active_platforms:
            cls = get_client_class(slug)
            if cls is None:
                # slug в registry есть (раз он попал в active_platforms),
                # но клиента под него ещё не написали — пропускаем.
                logger.warning("no client for platform %s", slug)
                continue
            # Дальше — конкретно от клиента: у HHClient — async with,
            # у других может быть просто конструктор.
            ...
"""
from __future__ import annotations

import logging
from typing import Iterator

from app.services.job_sites.base import (
    BaseJobSiteClient,
    all_registered_slugs,
    get_client_class,
)
from app.services.job_sites.registry import SUPPORTED_PLATFORM_SLUGS

logger = logging.getLogger("offerday.job_sites.dispatcher")


__all__ = [
    "BaseJobSiteClient",
    "get_client_class",
    "active_implementations",
    "verify_consistency",
]


def active_implementations() -> Iterator[tuple[str, type[BaseJobSiteClient]]]:
    """Возвращает пары ``(slug, cls)`` только для площадок, у которых:

    * есть запись в ``PLATFORMS_CATALOG`` (UI знает),
    * есть зарегистрированный клиент (импорт состоялся).
    """
    for slug in sorted(SUPPORTED_PLATFORM_SLUGS):
        cls = get_client_class(slug)
        if cls is not None:
            yield slug, cls


def verify_consistency() -> None:
    """Лог-хелпер: показать, по каким площадкам есть рассинхрон между
    UI-каталогом и набором импортированных клиентов.

    Зовём при старте приложения (см. ``main.lifespan``) — оно
    напечатает в лог список «есть в каталоге, но нет клиента» и
    «клиент есть, но в каталоге UI отсутствует».
    """
    catalog = SUPPORTED_PLATFORM_SLUGS
    registered = set(all_registered_slugs())

    missing_clients = sorted(catalog - registered)
    if missing_clients:
        logger.info(
            "platforms in UI catalog with no client implementation yet: %s",
            missing_clients,
        )

    orphan_clients = sorted(registered - catalog)
    if orphan_clients:
        logger.warning(
            "platform clients registered but not in UI catalog "
            "(add to registry.PLATFORMS_CATALOG or remove the class): %s",
            orphan_clients,
        )
