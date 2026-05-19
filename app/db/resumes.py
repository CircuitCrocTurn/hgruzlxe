"""Insert/upsert распарсенного резюме в таблицу ``resumes``.

Инлайн-парсер (``app.queue.jobs.pull_and_save_resume_for_user``) после
``HHClient.get_resume`` получает ``app.schemas.resume.Resume`` (Pydantic)
и зовёт :func:`resume_to_db`, чтобы положить или обновить ORM-
строку. Вся логика "одно резюме на юзера" живёт здесь — модель
``Resume`` в ``user_id``-колонке имеет UNIQUE.

В качестве ``file_path`` — обязательное NOT NULL поле — кладём
маркер ``"hh:<resume_id>"``: настоящего PDF мы не скачиваем, парсим
HTML/text-экспорт. Когда будет S3 для PDF — заменим.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Resume
from app.schemas.resume import Resume as ResumeSchema


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def resume_to_db(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    hh_resume_id: str,
    parsed: ResumeSchema,
) -> Resume:
    """Апсерт распарсенного резюме.

    Поведение — апсерт по ``user_id`` (схема предполагает одно
    резюме на юзера, см. unique-constraint в ``resumes.user_id``).
    На существующей строке обновляем parsed_data + full_txt.
    """
    res = await db.execute(select(Resume).where(Resume.user_id == user_id))
    row: Resume | None = res.scalar_one_or_none()

    file_path_marker = f"hh:{hh_resume_id}"
    parsed_data = parsed.model_dump()
    
    # ``parsed.raw_text`` — plain-text дамп резюме из ``HHClient
    # .get_resume`` (см. эндпоинт ``/resume_converter/resume.txt``).
    # Это то, что AI генератор сопроводительных хочет на вход
    # (см. ``app.services.cover_letter.generate_cover_letter``), и то,
    # что :class:`WorkerContext` отдаёт как ``resume_txt``.
    raw_text = (parsed.raw_text or "").strip() or None

    if row is None:
        row = Resume(
            user_id=user_id,
            # Имя колонки в схеме — ``file_path_pdf`` (см. миграцию
            # ``828ab3bd1f9d_check_by_hands.py``). Реального PDF мы
            # сейчас не сохраняем, кладём маркер ``hh:<resume_id>``.
            file_path_pdf=file_path_marker,
            parsed_data=parsed_data,
            full_txt=raw_text,
        )
        db.add(row)
    else:
        row.file_path_pdf = file_path_marker
        row.parsed_data = parsed_data
        # Не затираем ранее сохранённый full_txt пустотой: иногда
        # parsed.raw_text может прилететь None (например, если парсер
        # был успешен по структуре, но текст из hh.ru не получили).
        if raw_text is not None:
            row.full_txt = raw_text

    await db.flush()
    return row


async def hydrate_user_prefs_from_resume(
    db: AsyncSession,  # noqa: ARG001 — вызывающие коды передают сессию
    *,
    user_id: uuid.UUID,  # noqa: ARG001
    parsed: ResumeSchema,  # noqa: ARG001
) -> None:
    """No-op shim — сохранён для обратной совместимости.

    Раньше копировал имя / желаемую позицию в ``UserPreferences``.
    Этой таблицы больше нет — имена и титул читаются
    напрямую из ``resumes.parsed_data`` (см. ``PrefsView``).
    Функцию оставили пустым shim'ом, чтобы не ломать звавшие
    её места (PDF-парсер и хелпер ``pull_and_save_resume_for_user``).
    """
    return None
