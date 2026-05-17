"""Helper-функции вокруг :class:`PlatformCredential`.

Раньше cookies hh-сессии хранились на диске в
``var/hh_sessions/session_<phone>.pkl``. Это создавало два неприятных
момента:

* Деплой на другую машину / контейнер ронял сессии (файлов на новой
  машине нет → юзеру нужно перелогиниться).
* Бэкап БД не покрывал авторизованные сессии — приходилось руками
  бэкапить ещё и каталог pickle-файлов.

Теперь сессия живёт в БД в столбце ``encrypted_session`` (см. модель
:class:`PlatformCredential`). Уникальная пара ``(user_id, platform)``
=> один blob на юзера на площадку. Сам blob — это ``pickle.dumps`` от
``list(cookie_jar)`` со стороны клиента; что внутри blob'а — личное
дело подкласса ``BaseJobSiteClient``.

Шифрование blob'а выполняется в базовом классе клиента
(:mod:`app.services.job_sites.base`) — это место умеет про
``cryptography.fernet.Fernet`` и про SESSION_SECRET. Здесь мы видим
только сырые байты «как есть» (уже зашифрованные).

Публичный API:

* :func:`save_session_blob` — UPSERT-ом записать blob.
* :func:`load_session_blob` — достать blob (или ``None``, если нет).
* :func:`mark_platform_active` — апсерт active-строки без записи blob'а.
* :func:`get_active_credential_bytes` — достать ``encrypted_creds``
  активной строки (для token-based площадок).
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.platform_credential import PlatformCredential, STATUS_ACTIVE
from app.db.session import session_scope


async def mark_platform_active(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    platform: str,
    encrypted_creds: bytes = b"",
) -> PlatformCredential:
    """UPSERT строки ``(user_id, platform)`` со ``status='active'``.

    Если строки нет — создаём со ``status='active'``. Если есть —
    переключаем её в ``'active'`` (если была ``expired/error/...``).

    ``encrypted_creds`` обязательное поле в схеме (``nullable=False``);
    передаём ``b""`` для стуба, как и в UI-флоу. Когда дойдут руки до
    реального шифрования логин/пароля/токенов — здесь же и положим.
    """
    result = await db.execute(
        select(PlatformCredential).where(
            PlatformCredential.user_id == user_id,
            PlatformCredential.platform == platform,
        )
    )
    cred = result.scalar_one_or_none()
    if cred is None:
        cred = PlatformCredential(
            user_id=user_id,
            platform=platform,
            encrypted_creds=encrypted_creds,
            status=STATUS_ACTIVE,
        )
        db.add(cred)
    else:
        cred.status = STATUS_ACTIVE
    return cred


async def save_session_blob(
    *,
    user_id: uuid.UUID,
    platform: str,
    encrypted_blob: bytes,
) -> None:
    """Сохранить (или обновить) зашифрованный blob сессии площадки.

    Открывает собственный короткий ``session_scope``: вызывается из
    ``HHClient.__aexit__`` и других «вне-HTTP» путей, где AsyncSession
    наружу не пробрасывается. Если строки ``(user_id, platform)`` ещё
    нет — создаёт её со ``status='active'`` и пустыми
    ``encrypted_creds``.
    """
    async with session_scope() as db:
        cred = await mark_platform_active(
            db,
            user_id=user_id,
            platform=platform,
        )
        # mark_platform_active меняет только status, но не сам blob.
        # Поэтому явно перезаписываем оба поля.
        cred.encrypted_session = encrypted_blob


async def load_session_blob(
    *,
    user_id: uuid.UUID,
    platform: str,
) -> bytes | None:
    """Достать зашифрованный blob сессии активной строки.

    Возвращает ``None``, если активной строки нет или blob пустой
    (это значит, что юзер ещё не логинился на этой площадке либо
    сессия была явно очищена).
    """
    async with session_scope() as db:
        result = await db.execute(
            select(PlatformCredential.encrypted_session).where(
                PlatformCredential.user_id == user_id,
                PlatformCredential.platform == platform,
                PlatformCredential.status == STATUS_ACTIVE,
            )
        )
        blob = result.scalar_one_or_none()
        return blob or None


async def get_active_credential_bytes(
    user_id: uuid.UUID,
    platform: str,
) -> bytes | None:
    """Достать ``encrypted_creds`` активной строки юзера+площадки.

    Это «низкоуровневый» helper для token-based площадок: их
    ``from_context`` зовёт эту функцию, расшифровывает байты (когда
    будем шифровать) и кладёт распакованный токен в конструктор
    клиента. Если активной строки нет — возвращает ``None``.

    Открывает собственный короткий ``session_scope`` — чтобы
    ``from_context`` можно было звать из воркера без проброса
    AsyncSession (у воркера уже свой session_scope в
    ``load_worker_context``, мы не хотим вкладывать сессии).
    """
    async with session_scope() as db:
        result = await db.execute(
            select(PlatformCredential.encrypted_creds).where(
                PlatformCredential.user_id == user_id,
                PlatformCredential.platform == platform,
                PlatformCredential.status == STATUS_ACTIVE,
            )
        )
        return result.scalar_one_or_none()
