"""Базовый класс для всех ORM-моделей.

От него наследуются все модели в `app/db/models/*.py`. Сам по себе
ничего не делает — это просто корень иерархии для SQLAlchemy 2.0.

Что здесь важного, кроме самого `Base`:

`naming_convention` — шаблоны имён для индексов, уникальных ключей,
foreign keys, check constraints и primary keys. БЕЗ этого SQLAlchemy
полагается на дефолтные имена БД, и они отличаются от того, что
ожидает Alembic при autogenerate. На практике это выливается в
бесконечные "лишние" миграции типа "drop constraint without name,
create constraint with name" и в проблемы при `alembic downgrade`
(миграция не может удалить constraint, имя которого она не знает).

Конвенция ниже — стандартная из доков SQLAlchemy/Alembic. Один раз
заложил — забыл, всё работает само. Менять её потом БОЛЬНО (придётся
переименовывать constraints на проде), поэтому фиксируем сразу.

Шаблоны:
    ix_<table>_<column>             — индексы
    uq_<table>_<column>             — UNIQUE
    ck_<table>_<constraint>         — CHECK
    fk_<table>_<column>_<reftable>  — FOREIGN KEY
    pk_<table>                      — PRIMARY KEY
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Корневой класс всех ORM-моделей.

    Все модели в `app/db/models/*.py` наследуют от него — так SQLAlchemy
    знает, какие классы регистрировать как таблицы. Alembic тоже смотрит
    на `Base.metadata` для автогенерации миграций.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    def __repr__(self) -> str:
        # Дефолтный безопасный repr: использует `__dict__` напрямую, чтобы
        # не дёргать ленивую загрузку из БД при detached/expired инстансе.
        # Это критично для Starlette debug-middleware, который при
        # необработанном исключении вызывает ``repr()`` на локальных
        # переменных и иначе маскирует исходную ошибку
        # ``DetachedInstanceError``-ом из ленивой подгрузки.
        cls = type(self).__name__
        cols = []
        for key in ("id", "user_id"):
            if key in self.__dict__:
                cols.append(f"{key}={self.__dict__[key]!r}")
                break
        return f"<{cls} {' '.join(cols) or '?'}>"