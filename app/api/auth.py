"""
Регистрация / логин по SMS через hh.ru.

Двухэтапный флоу:
    POST /auth/request-code  { phone }   →  hh.ru шлёт SMS
    POST /auth/verify-code   { code }    →  проверяем код, поднимаем
                                            сессию, СТАВИМ фоновую
                                            задачу на парсинг резюме

Хранение cookies между двумя запросами:
    Раньше использовался pickle-файл на диске
    (``var/hh_sessions/session_<phone>.pkl``). Теперь — in-memory
    словарь :data:`app.services.job_sites.hh.hh._OTP_BRIDGE` (только
    на время OTP-флоу). После того, как ``verify-code`` создал/нашёл
    юзера в БД, cookies мигрируют в столбец
    ``platform_credentials.encrypted_session`` (тот же механизм, что
    использует воркер при последующих фоновых задачах).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.config import TWOCAPTCHA_KEY
from app.db.models import Resume
from app.db.session import get_db
from app.db.users import create_user_for_phone, find_by_phone
from app.queue import enqueue_resume_parse
from app.services.job_sites.hh import (
    HHAuthError,
    HHClient,
    _to_hh_phone,
    migrate_otp_bridge_to_db,
)

router = APIRouter(prefix="/auth", tags=["auth"])


class RequestCodeIn(BaseModel):
    phone: str = Field(..., min_length=10, max_length=20)


class VerifyCodeIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=10)


# ── утилиты ────────────────────────────────────────────────────────────────

def _normalize_phone_e164(raw: str) -> str | None:
    """Приводит произвольный пользовательский ввод к +7XXXXXXXXXX."""
    digits = "".join(c for c in raw if c.isdigit())
    if not (10 <= len(digits) <= 15):
        return None
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits


def _hh_error_to_detail(e: HHAuthError) -> str:
    """Маппим частые ключи ошибок hh.ru в коды, которые понимает фронт."""
    msg = str(e).lower()
    if "wrong" in msg or "invalid_code" in msg or "incorrect" in msg:
        return "invalid_code"
    if "expir" in msg:
        return "code_expired"
    if "captcha" in msg:
        return "captcha_failed"
    if "datadome" in msg or "_xsrf" in msg:
        return "hh_blocked"
    return f"hh_auth_error: {e}"


# ── эндпоинты ──────────────────────────────────────────────────────────────

@router.post("/request-code")
async def request_code(payload: RequestCodeIn, request: Request):
    """Шаг 1: отправка SMS через hh.ru.

    Внимание: при срабатывании капчи запрос может занять несколько минут
    (2captcha решает картинку). Фронту нужно показывать loader и не таймаутить
    раньше 3-5 минут.
    """
    e164 = _normalize_phone_e164(payload.phone)
    if not e164:
        raise HTTPException(400, "invalid_phone")
    if not TWOCAPTCHA_KEY:
        raise HTTPException(500, "twocaptcha_key_not_configured")

    hh_phone = _to_hh_phone(e164)

    try:
        async with HHClient(phone=hh_phone) as client:
            info = await client.begin_otp_login()
    except HHAuthError as e:
        raise HTTPException(400, _hh_error_to_detail(e))

    # запоминаем, какой именно номер сейчас в процессе верификации
    request.session["pending_phone"] = e164
    request.session["pending_hh_phone"] = hh_phone

    return {
        "ok": True,
        "formatted_phone": info["formatted_phone"],
        "code_length": info["code_length"],
        "next_send_in": info["next_send_in"],
    }


@router.post("/verify-code")
async def verify_code(
    payload: VerifyCodeIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Шаг 2: проверяем введённый код и поднимаем серверную сессию.

    Дальше развилка по наличию ``users.phone_number == e164`` в БД:

    1. **Существующий юзер (returning)** — никакого парсинга резюме.
       Достаём из БД его ``UserPreferences`` (имя/фамилия), кладём в
       сессию, отдаём фронту.
    2. **Новый юзер** — создаём строку ``users`` и запускаем
       inline-парсинг резюме через ``app.queue.enqueue_resume_parse``
       (``asyncio.create_task``). Имя/фамилия в ответе — пустые:
       появятся, когда фоновая задача распарсит резюме. Фронт
       может опрашивать ``GET /api/me/resume-job``.

    Если у возвращающегося юзера ``is_active=False`` (был
    ранее деактивирован) — возвращаем 403, чтобы он не
    мог войти. Это страховка на случай, если он успешно прошёл
    Stage-1 (телефон в БД), но мы всё равно не хотим пускать его
    в кабинет под этой учёткой.
    """
    e164 = request.session.get("pending_phone")
    hh_phone = request.session.get("pending_hh_phone")
    if not e164 or not hh_phone:
        raise HTTPException(400, "no_pending_request")

    hhuid: str | None = None
    db_user = None
    is_new_user = False
    cookies_migrated_via_aexit = False

    try:
        async with HHClient(phone=hh_phone) as client:
            await client.complete_otp_login(payload.code.strip())

            # Проверяем, что hh.ru действительно нас залогинил
            if not await client.is_authenticated():
                raise HHAuthError("login_did_not_take_effect")

            hhuid = client._cookie("hhuid")

            # ── Stage-1: новый или вернувшийся? ──────────────────────
            # Делаем ВНУТРИ блока ``async with`` специально: как
            # только узнали user_id, присваиваем его клиенту, и
            # ``__aexit__`` положит cookies сразу в БД (а не в
            # in-memory бридж). Это то, что заменило старую запись в
            # pickle-файл.
            db_user = await find_by_phone(db, e164)
            if db_user is not None and not db_user.is_active:
                # Юзер ранее был деактивирован (админом или
                # /api/settings/account/delete). Пускать нельзя.
                raise HTTPException(403, "account_disabled")

            is_new_user = db_user is None
            if db_user is None:
                # Новый юзер — создаём со связками
                # (Subscription/Preferences).
                db_user = await create_user_for_phone(
                    db,
                    e164_phone=e164,
                    first_name="",
                    last_name="",
                )

            # Промоутим клиента: после выхода из ``async with``
            # cookies лягут в platform_credentials.encrypted_session.
            client.user_id = db_user.id
            cookies_migrated_via_aexit = True

    except HHAuthError as e:
        raise HTTPException(400, _hh_error_to_detail(e))

    assert db_user is not None  # для type-checker'а

    # Подстраховка: если по какой-то причине ``__aexit__`` не сработал
    # (исключение между присвоением user_id и выходом из блока — на
    # практике не должно происходить, но защита дешёвая) — мигрируем
    # OTP-бридж в БД вручную.
    if not cookies_migrated_via_aexit:
        await migrate_otp_bridge_to_db(hh_phone, db_user.id)

    # Имя / email берём из ``resumes.parsed_data`` (единственный
    # источник после выпила ``user_preferences``). Для нового
    # юзера резюме ещё не распарсено (inline-таск только что
    # поставлен ниже), поэтому вернём пустые строки — фронт
    # подхватит их в следующем полле ``/api/me/resume-job``.
    resume_row = (
        await db.execute(select(Resume).where(Resume.user_id == db_user.id))
    ).scalar_one_or_none()
    parsed: dict[str, Any] = (
        resume_row.parsed_data if resume_row and resume_row.parsed_data else {}
    )
    full_name = (parsed.get("full_name") or "").strip()
    if full_name:
        parts = full_name.split()
        first_nm = parts[0]
        last_nm = " ".join(parts[1:]) if len(parts) > 1 else ""
    else:
        first_nm = ""
        last_nm = ""

    user_payload = {
        "id": f"hh_{hhuid or hh_phone}",
        "phone": e164,
        "first_name": first_nm,
        "last_name": last_nm,
        "email": parsed.get("email") or None,
    }

    # Серверная сессия — ставим её до DB-операций, чтобы при любом
    # дальнейшем сбое юзер хотя бы не оказался разлогинен после
    # удачного hh-логина.
    request.session.pop("pending_phone", None)
    request.session.pop("pending_hh_phone", None)
    request.session["user"] = user_payload
    request.session["user_db_id"] = str(db_user.id)
    request.session["hh_phone"] = hh_phone
    # Раньше тут ещё хранился ``request.session["hh_session_file"]`` —
    # путь к pickle. Больше не нужен: cookies теперь в БД, любой
    # эндпоинт восстанавливает клиент по user_id.
    request.session["logged_in_at"] = datetime.now(timezone.utc).isoformat()

    if is_new_user:
        # Только для новых юзеров: inline-парсинг резюме через
        # ``asyncio.create_task``. ``enqueue_resume_parse`` сама
        # закоммитит текущую транзакцию (`db`-параметр), чтобы
        # воркер в своём ``session_scope`` увидел свежий ``User`` /
        # ``platform_credentials``.
        await enqueue_resume_parse(
            db,
            user_id=db_user.id,
            hh_phone=hh_phone,
        )

    return {
        "ok": True,
        "user": user_payload,
        "is_new_user": is_new_user,
    }


@router.post("/logout")
async def logout(request: Request):
    """Чистим серверную сессию у себя. cookies hh.ru, лежащие в
    ``platform_credentials.encrypted_session``, **намеренно не
    трогаем** — их можно переиспользовать при следующем логине, чтобы
    не дёргать капчу лишний раз. Если нужно полностью разорвать связь
    с hh.ru — придётся отдельным запросом обнулить строку в
    ``platform_credentials``.
    """
    request.session.clear()
    return {"ok": True}


@router.post("/refresh")
async def refresh(request: Request):
    """В hh-флоу нет refresh-токена. Если cookies протухли, HHClient сам
    перелогинит при следующем запросе через is_authenticated() → login().
    Эндпоинт оставлен под будущее (например, проактивный прогрев сессии)."""
    raise HTTPException(501, "not_implemented")
