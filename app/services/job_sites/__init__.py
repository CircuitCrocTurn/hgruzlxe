"""Job-sites: подсистема клиентов работных площадок.

Импорты в этом __init__ нужны исключительно для того, чтобы
сработал ``__init_subclass__`` в ``BaseJobSiteClient`` и каждый
клиент зарегистрировался в общем диспатчере. Без этих импортов
Python никогда не выполнит ``class HHClient(...)`` и
``get_client_class("hh")`` будет возвращать ``None``.

При добавлении новой площадки — просто допиши сюда импорт нужного
клиента (например, ``from app.services.job_sites.habr.client import
HabrClient``). Linter будет ругаться на «unused import» — это
нормально, можно либо игнорировать через ``# noqa``, либо
дописать модуль в ``__all__``.
"""
from __future__ import annotations

# noqa: F401 — импорт нужен только для регистрации клиента в
# диспатчере через ``__init_subclass__``.
from app.services.job_sites.hh import HHClient  # noqa: F401
from app.services.job_sites.habr import HabrClient  # noqa: F401
from app.services.job_sites.superjob import SuperJobClient  # noqa: F401

__all__ = ["HHClient", "HabrClient", "SuperJobClient"]
