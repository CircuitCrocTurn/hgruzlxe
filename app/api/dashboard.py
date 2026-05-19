"""HTTP API маршруты для авторизованных страниц дашборда.

Все маршруты требуют сессии (см. :func:`_require_user`).  Они тонкие:
получают payload, лежащий в JSON-body или ``multipart/form-data``,
обновляют соответствующие строки в БД и возвращают компактный JSON
(``{"ok": True, ...}``).
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PROJECT_ROOT
from app.db.dashboard_data import (
    build_active_chat_ctx,
    get_response_cards_and_stats,
    chats_status_counts,
    list_chat_cards,
)
from app.db.models import (
    Chat,
    Message,
    PlatformCredential,
    Resume,
    Subscription,
    User,
)
from app.db.models.chat import SERVICE_HABR, SERVICE_HH
from app.db.models.platform_credential import STATUS_ACTIVE as PC_STATUS_ACTIVE
from app.db.models.subscription import (
    PLAN_BASIC,
    PLAN_FREE,
    PLAN_PRO,
    STATUS_ACTIVE,
    STATUS_TRIAL,
)
from app.db.session import get_db
from app.db.users import ensure_platform_credentials, ensure_user_for_session
from app.queue import (
    enqueue_resume_doc_parse_job,
    enqueue_resume_parse,
    get_resume_parse_status,
)
from app.services.realtime import notify_chat_change
from app.services.job_sites.habr.auth_flow import (
    HabrFlowError,
    cancel_habr_connect_flow,
    start_habr_connect_flow,
    verify_habr_connect_flow,
)
from app.services.job_sites.registry import (
    PLATFORM_HABR,
    PLATFORM_HH,
    is_supported as is_supported_platform,
)

router = APIRouter(prefix="/api", tags=["dashboard"])

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────


async def _require_user(request: Request, db: AsyncSession) -> User:
    """Bounce unauthenticated callers with 401, otherwise return User.

    Состояния и реакции:

    * Нет ``session["user"]`` (никогда не логинился / уже разлогинился)
      → 401 ``not_authenticated``.
    * ``session["user"]`` есть, но в БД нет юзера с таким
      ``phone_number`` (например, БД почистили, а кука в браузере
      осталась подписанной) → чистим сессию, 401 ``stale_session``.
    * Юзер есть, но ``is_active=False`` (Stage-2 пометил как дубль)
      → чистим сессию, 401 ``account_disabled``. Фронт уведёт на
      ``/login`` без модалок (E=1), а в логах остаётся запись Stage-2.

    Во всех 401-ветках фронтенд (см. ``app/templates/login.html`` и
    обёртки на дашборде) сам редиректит на ``/login`` — мы здесь
    только говорим «иди логиниться заново».
    """
    session_user = request.session.get("user")
    if not session_user:
        raise HTTPException(status_code=401, detail="not_authenticated")
    user = await ensure_user_for_session(db, session_user)
    if user is None:
        # Кука валидна (подписана нашим SESSION_SECRET), но строки в
        # БД больше нет. Сбрасываем сессию: следующий GET без куки
        # сразу попадёт в ``not_authenticated``.
        request.session.clear()
        raise HTTPException(status_code=401, detail="stale_session")
    if not user.is_active:
        request.session.clear()
        raise HTTPException(status_code=401, detail="account_disabled")
    return user


def _otp_for(_platform: str) -> str:
    """Generate a 4-digit OTP (random per request)."""
    n = secrets.randbelow(10_000)
    return f"{n:04d}"


def _filter_str(value: Any, max_len: int = 1000) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s[:max_len]


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.lower() in ("1", "true", "on", "yes", "да")
    return False


# ── Job control: /responses Start / Pause ────────────────────────────


@router.post("/jobs/start")
async def jobs_start(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    user.auto_apply_running = True
    return {"ok": True, "running": True}


@router.post("/jobs/pause")
async def jobs_pause(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    user.auto_apply_running = False
    return {"ok": True, "running": False}


# ── /responses live updates ──────────────────────────────────────────
# Браузер раз в ~20 сек дёргает этот эндпоинт и до-рендерит новые
# карточки сверху, без перезагрузки страницы. Подробности —
# в нижнем `<script>` ``responses.html``.
@router.get("/responses")
async def responses_feed(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    return await get_response_cards_and_stats(db, user)


# ── Settings: profile (имя/email/телефон/локация) ────────────────────


@router.post("/settings/profile")
async def settings_profile(request: Request, db: AsyncSession = Depends(get_db)):
    """Сохраняет правки профиля (имя / email / телефон) из формы /settings.

    ``PrefsView`` собирает имя / контакты из ``resumes.parsed_data``,
    поэтому правки кладём туда же: новое резюме перезапишет их при
    следующем парсинге, но до этого они «приклеиваются» к юзеру.
    ``email`` / ``phone_number`` дополнительно пишутся прямо в
    ``users`` (туда смотрит auth-флоу и hh-bridge).
    """
    user = await _require_user(request, db)
    payload = await request.json()
    first_name = (payload.get("first_name") or "").strip()
    last_name = (payload.get("last_name") or "").strip()
    email = (payload.get("email") or "").strip()
    phone = (payload.get("phone") or "").strip()

    if email:
        existing = (
            await db.execute(
                select(User).where(
                    User.email == email,
                    User.id != user.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise HTTPException(
                status_code=400,
                detail={"error": "email_taken", "message": "email уже занят"},
            )
        user.email = email

    # Телефон НЕ пишем в ``users.phone_number`` — это идентификатор
    # сессии (см. ``find_by_phone`` в ``app/db/users.py``); поменяй
    # его руками — и юзер мгновенно потеряет вход. Кладём в
    # ``parsed_data["phone"]`` (откуда читает ``PrefsView.contact_phone``)
    # — этого хватает, чтобы поле отображалось «новым».
    overrides: dict[str, Any] = {
        "full_name": " ".join(p for p in (first_name, last_name) if p),
    }
    if phone:
        overrides["phone"] = phone
    if email:
        overrides["email"] = email
    await _save_resume_overrides(db, user, overrides)
    return {"ok": True}


# ── Settings: search preferences ─────────────────────────────────────


@router.post("/settings/preferences")
async def settings_preferences(request: Request, db: AsyncSession = Depends(get_db)):
    """Сохраняет правки предпочтений (должность / стек / зарплата / …).

    Кладёт значения в ``resumes.parsed_data`` (источник для
    ``PrefsView``). При следующем фоновом парсинге резюме их
    перепишет — это окей: юзер всегда может поправить их снова.
    """
    user = await _require_user(request, db)
    payload = await request.json()

    stack_raw = payload.get("stack")
    if isinstance(stack_raw, str):
        skills = [s.strip() for s in stack_raw.split(",") if s.strip()]
    elif isinstance(stack_raw, list):
        skills = [str(s).strip() for s in stack_raw if str(s).strip()]
    else:
        skills = None

    work_formats_raw = payload.get("work_formats")
    if isinstance(work_formats_raw, list):
        work_formats = [str(x).strip() for x in work_formats_raw if str(x).strip()]
    elif isinstance(work_formats_raw, str) and work_formats_raw.strip():
        work_formats = [work_formats_raw.strip()]
    else:
        work_formats = None

    updates: dict[str, Any] = {}
    if "position" in payload:
        updates["title"] = (payload.get("position") or "").strip() or None
    if skills is not None:
        updates["skills"] = skills
    if "salary" in payload:
        updates["salary"] = (payload.get("salary") or "").strip() or None
    if work_formats is not None:
        updates["work_format"] = work_formats
    if "grade" in payload:
        updates["grade"] = (payload.get("grade") or "").strip() or None
    if "stop_words" in payload:
        updates["stop_words"] = (payload.get("stop_words") or "").strip() or None
    if "stop_companies" in payload:
        updates["stop_companies"] = (
            payload.get("stop_companies") or ""
        ).strip() or None

    await _save_resume_overrides(db, user, updates)
    return {"ok": True}


async def _save_resume_overrides(
    db: AsyncSession,
    user: User,
    updates: dict[str, Any],
) -> Resume | None:
    """Записать ручные правки в ``resumes.parsed_data`` (создаст строку при
    необходимости). Возвращает обновлённый ``Resume`` или ``None``, если
    ``updates`` пустой.

    PrefsView читает ``Resume.parsed_data`` — без этой записи правки
    с /settings терялись бы при следующем рендере страницы.
    """
    if not updates:
        return None
    resume = (
        await db.execute(select(Resume).where(Resume.user_id == user.id))
    ).scalar_one_or_none()
    if resume is None:
        resume = Resume(
            user_id=user.id,
            parsed_data=dict(updates),
            # ``file_path_pdf`` объявлен NOT NULL; маркер ``manual:``
            # отличает «руками заполненный» профиль от настоящих
            # резюме-файлов и hh-резюме (``hh:<hash>``).
            file_path_pdf="manual:settings",
        )
        db.add(resume)
        await db.flush()
        return resume
    current = dict(resume.parsed_data or {})
    current.update(updates)
    resume.parsed_data = current
    # SQLAlchemy не видит in-place мутации JSONB — явно подменяем
    # объект, чтобы commit действительно ушёл в БД.
    return resume


# ── Settings: notifications ──────────────────────────────────────────


@router.post("/settings/notifications")
async def settings_notifications(request: Request, db: AsyncSession = Depends(get_db)):
    user = await _require_user(request, db)
    payload = await request.json()

    # На юзере живут только 2 флага: email + telegram. Остальные
    # (push/messages/weekly) — фичи под них не было, просто принимаем
    # и игнорируем, чтобы не ломать фронт.
    user.notif_email = _bool(payload.get("email", user.notif_email))
    user.notif_telegram = _bool(payload.get("telegram", user.notif_telegram))
    return {"ok": True}


# ── Settings: cover letter ───────────────────────────────────────────


@router.post("/settings/cover-letter")
async def settings_cover_letter(request: Request, db: AsyncSession = Depends(get_db)):
    """No-op stub. Шаблоны сопроводительных (таблица
    ``cover_letter_templates``) выпилены: сопроводительные генерирует
    LLM по резюме и описанию вакансии (см.
    ``app.services.cover_letter.generate_cover_letter``). Принимаем
    запрос от фронта и игнорируем.
    """
    await _require_user(request, db)
    try:
        await request.json()
    except Exception:
        pass
    return {"ok": True}


# ── Settings: language / region ──────────────────────────────────────


@router.post("/settings/language")
async def settings_language(request: Request, db: AsyncSession = Depends(get_db)):
    """No-op stub. Язык / валюта / таймзона раньше хранились
    на user_preferences и бэком нигде не использовались — вырезаны
    вместе с таблицей.
    """
    await _require_user(request, db)
    return {"ok": True}


# ── Settings: security (password / 2fa stubs) ────────────────────────


@router.post("/settings/security/password")
async def settings_password(request: Request, db: AsyncSession = Depends(get_db)):
    """Stub for "сменить пароль" — hh-flow has no password yet, so we
    just accept the request and return ok. The UI shows a confirmation."""
    await _require_user(request, db)
    payload = await request.json()
    new_password = payload.get("new_password") or ""
    if len(str(new_password)) < 8:
        raise HTTPException(status_code=400, detail="password_too_short")
    return {"ok": True, "message": "password_updated"}


@router.post("/settings/security/2fa")
async def settings_2fa(request: Request, db: AsyncSession = Depends(get_db)):
    """No-op stub. 2FA раньше хранился в user_preferences, реальных
    фич не было. Всегда отвечаем ``enabled=false``.
    """
    await _require_user(request, db)
    return {"ok": True, "enabled": False}


# ── Settings: telegram link token ────────────────────────────────────


@router.post("/settings/telegram/token")
async def settings_telegram_token(request: Request, db: AsyncSession = Depends(get_db)):
    """Генерирует deep-link токен для Telegram-бота.

    После выпила ``user_preferences`` хранить токен негде —
    выдаём свежий при каждом вызове. Реальная верификация
    /start <token> на стороне бота пока не реализована — это UI-заглушка.
    """
    await _require_user(request, db)
    try:
        await request.json()
    except Exception:
        pass
    token = secrets.token_hex(8)
    return {
        "ok": True,
        "token": token,
        "deep_link": f"/start offerday_{token}",
    }


# ── Settings: platforms — connect/verify/disconnect ──────────────────


# We don't ship any real OTP transport in this build — clicking
# "Подключить" creates / refreshes a row with status ``captcha_required``
# (a real flow would set ``pending_otp``) and stores the expected code in
# an in-process map. ``verify`` simply compares the user-entered code
# with what we generated.

# In-memory map (per worker process): user_id -> {platform: expected_code}.
# Раньше хранилось в ``user_preferences.pending_platform_otps``;
# таблица ушла — демо-флоу остаётся локальным для процесса.
_PENDING_OTPS: dict[uuid.UUID, dict[str, str]] = {}


@router.post("/settings/platforms/connect")
async def settings_platforms_connect(
    request: Request, db: AsyncSession = Depends(get_db)
):
    user = await _require_user(request, db)
    payload = await request.json()
    platform = _filter_str(payload.get("platform"), 64)
    if not platform:
        raise HTTPException(status_code=400, detail="platform_required")
    if not is_supported_platform(platform):
        # Левый slug — клиент пытается подключить площадку, которой
        # нет в registry. Не записываем — это бы засорило БД и
        # потом воркер всё равно её отфильтровал бы.
        raise HTTPException(status_code=400, detail="unsupported_platform")

    # ── Habr: спец-флоу через temp.coda.ink ────────────────────────
    # У habr подключение нетривиальное: либо регистрируем аккаунт на
    # временную почту и затем меняем email на основной, либо (если
    # аккаунт уже существует) восстанавливаем пароль. Вся логика —
    # в ``app.services.job_sites.habr.auth_flow``; здесь просто
    # достаём контекст юзера и зовём orchestrator.
    if platform == PLATFORM_HABR:
        # На случай, если юзер старый и temp_email/platform_password
        # ещё не заполнены — лениво дозальём (см. документацию функции).
        await ensure_platform_credentials(db, user)

        # Основной email юзера ищем в распарсенном резюме. ``users.email``
        # — синтетика вида ``8XXXXXXXXXX@hh.local``, она нам не нужна.
        resume_row = (
            await db.execute(select(Resume).where(Resume.user_id == user.id))
        ).scalar_one_or_none()
        parsed: dict[str, Any] = (
            resume_row.parsed_data if resume_row and resume_row.parsed_data else {}
        )
        main_email = (parsed.get("email") or "").strip() or None

        try:
            flow_payload = await start_habr_connect_flow(
                user_id=user.id,
                main_email=main_email,
                temp_email=user.temp_email,
                platform_password=user.platform_password or "",
            )
        except HabrFlowError as e:
            raise HTTPException(status_code=400, detail=e.code)

        # UPSERT credential — status=captcha_required, чтобы карточка
        # в UI перешла в «жёлтое» состояние «жду код».
        result = await db.execute(
            select(PlatformCredential).where(
                PlatformCredential.user_id == user.id,
                PlatformCredential.platform == platform,
            )
        )
        cred = result.scalar_one_or_none()
        if cred is None:
            cred = PlatformCredential(
                user_id=user.id,
                platform=platform,
                encrypted_creds=b"",
                status="captcha_required",
            )
            db.add(cred)
        else:
            cred.status = "captcha_required"

        return {
            "ok": True,
            "message": "code_sent",
            **flow_payload,
        }

    # ── Дефолтный путь для остальных площадок (демо-OTP) ───────────
    code = _otp_for(platform)
    _PENDING_OTPS.setdefault(user.id, {})[platform] = code

    # Upsert credential row.
    result = await db.execute(
        select(PlatformCredential).where(
            PlatformCredential.user_id == user.id,
            PlatformCredential.platform == platform,
        )
    )
    cred = result.scalar_one_or_none()
    if cred is None:
        cred = PlatformCredential(
            user_id=user.id,
            platform=platform,
            encrypted_creds=b"",
            status="captcha_required",
        )
        db.add(cred)
    else:
        cred.status = "captcha_required"

    # In a real build we'd send the code over SMS / OAuth / e-mail here.
    # For the stub we expose it in the response so the demo flow works.
    return {
        "ok": True,
        "code_length": 4,
        "demo_code": code,
        "message": "code_sent",
    }


@router.post("/settings/platforms/verify")
async def settings_platforms_verify(
    request: Request, db: AsyncSession = Depends(get_db)
):
    user = await _require_user(request, db)
    payload = await request.json()
    platform = _filter_str(payload.get("platform"), 64)
    code = _filter_str(payload.get("code"), 16)
    if not platform or not code:
        raise HTTPException(status_code=400, detail="platform_and_code_required")
    if not is_supported_platform(platform):
        raise HTTPException(status_code=400, detail="unsupported_platform")

    # ── Habr: подтверждение кода завершает один из двух подфлоу ────
    if platform == PLATFORM_HABR:
        try:
            await verify_habr_connect_flow(
                user_id=user.id,
                code=code,
                platform_password=user.platform_password or "",
            )
        except HabrFlowError as e:
            # Маппим бизнес-ошибку в HTTP-код. ``invalid_code`` — 400,
            # фронт показывает «неверный код»; остальные тоже 400,
            # фронт показывает дефолтный «не удалось подтвердить».
            raise HTTPException(status_code=400, detail=e.code)

        result = await db.execute(
            select(PlatformCredential).where(
                PlatformCredential.user_id == user.id,
                PlatformCredential.platform == platform,
            )
        )
        cred = result.scalar_one_or_none()
        if cred is None:
            cred = PlatformCredential(
                user_id=user.id,
                platform=platform,
                encrypted_creds=b"",
                status="active",
            )
            db.add(cred)
        else:
            cred.status = "active"
        return {"ok": True, "status": "active"}

    # ── Дефолтный путь для остальных площадок ──────────────────────
    pending = _PENDING_OTPS.get(user.id, {})
    expected = pending.get(platform)
    if not expected:
        raise HTTPException(status_code=400, detail="no_pending_code")
    if code.strip() != expected:
        raise HTTPException(status_code=400, detail="invalid_code")

    pending.pop(platform, None)

    result = await db.execute(
        select(PlatformCredential).where(
            PlatformCredential.user_id == user.id,
            PlatformCredential.platform == platform,
        )
    )
    cred = result.scalar_one_or_none()
    if cred is None:
        cred = PlatformCredential(
            user_id=user.id,
            platform=platform,
            encrypted_creds=b"",
            status="active",
        )
        db.add(cred)
    else:
        cred.status = "active"
    return {"ok": True, "status": "active"}


@router.post("/settings/platforms/disconnect")
async def settings_platforms_disconnect(
    request: Request, db: AsyncSession = Depends(get_db)
):
    user = await _require_user(request, db)
    payload = await request.json()
    platform = _filter_str(payload.get("platform"), 64)
    if not platform:
        raise HTTPException(status_code=400, detail="platform_required")
    if not is_supported_platform(platform):
        raise HTTPException(status_code=400, detail="unsupported_platform")

    result = await db.execute(
        select(PlatformCredential).where(
            PlatformCredential.user_id == user.id,
            PlatformCredential.platform == platform,
        )
    )
    cred = result.scalar_one_or_none()
    if cred is not None:
        cred.status = "disabled"

    _PENDING_OTPS.get(user.id, {}).pop(platform, None)
    # Habr-флоу хранится в отдельной in-memory мапе — гасим и его.
    if platform == PLATFORM_HABR:
        cancel_habr_connect_flow(user.id)
    return {"ok": True, "status": "disabled"}


# ── Settings: resume ─────────────────────────────────────────────────


_RESUMES_DIR = PROJECT_ROOT / "var" / "resumes"


def _ensure_resume_dir() -> Path:
    _RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    return _RESUMES_DIR


@router.post("/settings/resume/url")
async def settings_resume_url(request: Request, db: AsyncSession = Depends(get_db)):
    """Сохранить URL резюме + (если URL непустой и в сессии есть
    hh_phone) поставить фоновую задачу на парсинг этого резюме.

    При нажатии кнопки «Сохранить изменения» в Settings → Резюме
    фронт послает сюда ``{url: "https://hh.ru/resume/<hash>"}``. Сразу
    же стартует тот же pipeline, что и после подтверждения SMS-кода
    для нового юзера: воркер ходит на hh.ru, парсит резюме, апсёртит
    ``resumes.parsed_data``, дёргает Stage-2 identity-check.

    Контракт ответа:

    * ``ok``        — True при удачном сохранении.
    * ``profile_url`` — нормализованный URL (или null, если очистили).
    * ``job_id``    — UUID запущенной/переиспользованной задачи или null.
    * ``parsing``   — True, если фоновая задача поставлена; False, если
                      только сохранили URL без запуска (см. ``reason``).
    * ``reason``    — для UI: причина, почему парсинг не стартовал
                      (``empty_url`` | ``unsupported_platform`` |
                      ``no_hh_session``). Не выставляется, если
                      ``parsing=True``.

    Распознавание платформы делается по самому URL:

    * hh.ru → ставим job в очередь, если в сессии есть ``hh_phone``;
    * остальное (LinkedIn и т.п.) → пока не поддерживаем — сохраняем
      URL, но честно говорим ``reason="unsupported_platform"``,
      чтобы фронт мог показать понятное сообщение, а не «крутить
      колесо» в надежде на парсинг.
    """
    user = await _require_user(request, db)
    payload = await request.json()
    url = _filter_str(payload.get("url"), 512)
    if url is None:
        # Очистка URL: profile_url больше не хранится отдельно
        # (это было поле на user_preferences). Ответ оставляем
        # совместимым с фронтом.
        return {
            "ok": True,
            "profile_url": None,
            "job_id": None,
            "parsing": False,
            "reason": "empty_url",
        }

    # Lightweight sanity check — must look like a URL.
    if not re.match(r"^https?://", url):
        raise HTTPException(status_code=400, detail="invalid_url")

    # Платформа hh.ru — то, что мы умеем парсить через
    # ``pull_and_save_resume_for_user``. Для прочих доменов
    # (LinkedIn и т.п.) пока просто сохраняем URL — фронт
    # покажет «формат пока не поддерживается,
    # попробуйте загрузить PDF в дроп-зону».
    if "hh.ru" not in url:
        return {
            "ok": True,
            "profile_url": url,
            "job_id": None,
            "parsing": False,
            "reason": "unsupported_platform",
        }

    # Приоритет источников телефона:
    # 1) ``hh_phone`` из cookie-сессии (свежий логин в браузере);
    # 2) ``user.phone_number`` из БД — если cookie-сессия пересоздалась
    #    или юзер зашёл с другого устройства, а на hh.ru он уже
    #    логинился: cookies живут в ``platform_credentials
    #    .encrypted_session`` по ``user_id``, а сам ``hh_phone`` нам
    #    нужен только для per-user lock и как «лейбл» для HHClient.
    # Аналогично делает ``/api/chats/sync`` (см. ``_hh_phone_for``).
    hh_phone = _hh_phone_for(user, request)
    if not hh_phone:
        # Реально первый раз — ни в сессии, ни в БД телефона нет.
        # Фронт по ``reason`` покажет «войдите через SMS заново».
        return {
            "ok": True,
            "profile_url": url,
            "job_id": None,
            "parsing": False,
            "reason": "no_hh_session",
        }

    # Очередь убрана: парсим прямо в текущем event-loop'е
    # через ``asyncio.create_task``. Идемпотентность держится в
    # самой ``enqueue_resume_parse`` (если уже крутится — вернёт
    # тот же таск). Перед спавном таска она же закоммитит сессию,
    # чтобы воркер увидел свежие данные.
    await enqueue_resume_parse(
        db,
        user_id=user.id,
        hh_phone=hh_phone,
        resume_url=url,
    )
    return {
        "ok": True,
        "profile_url": url,
        "job_id": None,
        "parsing": True,
    }


@router.post("/settings/resume/upload")
async def settings_resume_upload(
    request: Request,
    file: UploadFile = File(...),
    title: str | None = Form(None),  # noqa: ARG001 — legacy contract; не сохраняем
    db: AsyncSession = Depends(get_db),
):
    """Принять загруженный файл-резюме (PDF / DOC / DOCX / TXT) и
    запустить фоновый парсинг через Anthropic Claude.

    Контракт ответа:

    * ``ok`` — всегда True при удачной загрузке файла.
    * ``filename`` — оригинальное имя файла.
    * ``parsing`` — всегда True (фоновый таск парсинга поставлен).
    """
    user = await _require_user(request, db)
    if not file.filename:
        raise HTTPException(status_code=400, detail="filename_required")
    suffix = Path(file.filename).suffix.lower()
    if suffix and suffix not in {".pdf", ".doc", ".docx", ".txt"}:
        raise HTTPException(status_code=400, detail="unsupported_file_type")

    target_dir = _ensure_resume_dir()
    storage_name = f"{user.id}_{uuid.uuid4().hex}{suffix}"
    target_path = target_dir / storage_name
    with target_path.open("wb") as out:
        while True:
            chunk = await file.read(64 * 1024)
            if not chunk:
                break
            out.write(chunk)

    file_path_rel = str(target_path.relative_to(PROJECT_ROOT))

    # Resume.user_id is unique — upsert.
    result = await db.execute(select(Resume).where(Resume.user_id == user.id))
    resume = result.scalar_one_or_none()

    if resume is None:
        resume = Resume(
            user_id=user.id,
            file_path_pdf=file_path_rel,
        )
        db.add(resume)
    else:
        # Try to drop the previous file (best-effort).
        old = PROJECT_ROOT / resume.file_path_pdf
        if old.exists() and old.is_file() and old != target_path:
            try:
                old.unlink()
            except OSError:
                pass
        resume.file_path_pdf = file_path_rel
        # Новый файл → инвалидируем результаты прошлого парсинга,
        # чтобы фоновый таск отработал заново и заменил parsed_data.
        resume.parsed_at = None

    await db.flush()
    resume_id = resume.id
    file_mime = file.content_type or ""

    # Запускаем парсер fire-and-forget. Воркер сам откроет свою
    # session_scope, чтобы коммит не зависел от текущего request-
    # скоупного db.
    enqueue_resume_doc_parse_job(
        resume_id=resume_id,
        user_id=user.id,
        file_path_rel=file_path_rel,
        mime_type=file_mime,
        filename=file.filename,
    )

    return {
        "ok": True,
        "filename": file.filename,
        "parsing": True,
    }


# ── Settings: account ────────────────────────────────────────────────


@router.post("/settings/account/delete")
async def settings_account_delete(
    request: Request, db: AsyncSession = Depends(get_db)
):
    user = await _require_user(request, db)
    user.is_active = False
    request.session.clear()
    return {"ok": True, "message": "account_deactivated"}


# ── Settings: subscription / tariffs ─────────────────────────────────


_VALID_PLANS = {PLAN_FREE, PLAN_BASIC, PLAN_PRO}


@router.post("/settings/subscription/select")
async def settings_subscription_select(
    request: Request, db: AsyncSession = Depends(get_db)
):
    """Switch the user's subscription plan.

    Payment integration is intentionally a stub here — we just flip
    ``Subscription.plan`` and mark the row active.  When YooKassa
    or another processor lands, this endpoint becomes the place to
    redirect the client to the checkout URL.
    """
    user = await _require_user(request, db)
    payload = await request.json()
    plan = (payload or {}).get("plan", "").strip().lower()
    if plan not in _VALID_PLANS:
        raise HTTPException(status_code=400, detail="unknown_plan")

    sub = (
        await db.execute(select(Subscription).where(Subscription.user_id == user.id))
    ).scalar_one_or_none()
    if sub is None:
        sub = Subscription(user_id=user.id, plan=plan, status=STATUS_TRIAL)
        db.add(sub)
    else:
        sub.plan = plan
        sub.status = STATUS_ACTIVE if plan != PLAN_FREE else STATUS_TRIAL

    await db.commit()
    label = {"free": "Free", "basic": "Basic", "pro": "Pro"}[plan]
    return {
        "ok": True,
        "plan": plan,
        "message": f"План «{label}» активирован",
    }


# ── Resume background job status ────────────────────────────────────
#
# После ``/auth/verify-code`` фронт может опросить этот эндпоинт,
# чтобы понять, готово ли распарсенное резюме (status="done"), идёт
# ли парсинг (status="running"|"queued") или упало (status="failed").
#
# Поллинг каждые 1.5–3 секунды, пока статус ∈ queued|running.
# В ``done`` придёт ``result.user_name`` / ``result.title`` —
# можно сразу обновить шапку с именем и (опционально) показать
# распарсенный профиль.


@router.get("/me/resume-job")
async def me_resume_job(request: Request, db: AsyncSession = Depends(get_db)):
    """Статус последней фоновой задачи парсинга резюме текущего юзера.

    Ответ:
        ``{"status": null}`` — задач не было ни одной;
        ``{"id": ..., "status": "queued|running|done|failed", ...}``
        — последняя задача (по ``created_at`` desc).
    """
    user = await _require_user(request, db)
    # Очереди больше нет — статус читаем из in-memory map'а, который
    # обновляет ``enqueue_resume_parse``. Если процесс рестартанулся
    # пока юзер был на странице, вернётся ``status=null`` — фронт
    # это трактует как «парсинг закончен / не запускался», что
    # совпадает с тем, как мы выходили из удалённой очереди.
    status = get_resume_parse_status(user.id)
    if status is None:
        return {"status": None}
    return {"id": None, **status}


@router.post("/me/resume/refresh")
async def me_resume_refresh(
    request: Request, db: AsyncSession = Depends(get_db),
):
    """Ручной рефреш резюме из настроек.

    Логика — та же, что у автоматической постановки в ``verify-code``
    для нового юзера: запускаем inline-парсинг через
    ``enqueue_resume_parse``. Если у юзера уже крутится таск
    парсинга — переиспользуем его и возвращаем ``reused=true``.

    По успеху парсинга воркер сам дёрнет
    ``run_identity_check_job`` (Stage-2) — точно как и для нового
    юзера. Это полезно, например, если пользователь поправил резюме
    на hh.ru и теперь нужно перепроверить email/ФИО.

    HTTP-контракт:
        * 200 ``{"ok": true, "job_id": null, "reused": bool}`` —
          ``reused=true`` означает, что на момент вызова уже шёл
          парсинг (новый таск не спавнили). Поле ``job_id`` всегда
          ``null`` — очередь убрана, UUID-ом теперь нечего отдавать.
        * 400 ``"no_hh_session"`` — в серверной сессии нет ``hh_phone``
          (например, юзер не логинился или браузерная сессия
          истекла). Юзеру нужно перелогиниться.
    """
    user = await _require_user(request, db)

    # cookies живут в platform_credentials.encrypted_session, их
    # вытянет воркер по user_id — но без hh_phone некуда поставить
    # per-user lock. Берём из сессии, иначе фоллбэчимся на
    # ``user.phone_number`` из БД (тот же приём, что в ``_hh_phone_for``
    # для ``/api/chats/sync``).
    hh_phone = _hh_phone_for(user, request)
    if not hh_phone:
        raise HTTPException(400, "no_hh_session")

    # Очередь убрана: запускаем парсинг inline. Если для юзера уже
    # крутится таск — ``enqueue_resume_parse`` его переиспользует
    # (см. idempotency-чек по ``_INFLIGHT_RESUME_TASKS``). Поле
    # ``reused`` оставляем в ответе для обратной совместимости
    # фронта: True, если на момент вызова уже что-то крутилось.
    pre_status = get_resume_parse_status(user.id) or {}
    pre_running = pre_status.get("status") == "running"

    await enqueue_resume_parse(
        db,
        user_id=user.id,
        hh_phone=hh_phone,
    )

    return {
        "ok": True,
        "job_id": None,
        "reused": pre_running,
    }


# ── /api/chats ─────────────────────────────────────────────────────────
@router.post("/chats/sync")
async def chats_sync(request: Request, db: AsyncSession = Depends(get_db)):
    """Синхронизация чатов с hh.ru и career.habr.com.

    Дёргается фронтом ``chats.html`` при лоаде страницы. Запускает
    параллельно sync для всех площадок, которые у юзера активны
    в ``platform_credentials`` (``status='active'``):

    * **hh.ru** — ``app.services.job_sites.hh.chats_sync.sync_hh_chats_for_user``
      ходит на ``chatik.hh.ru/chatik/api/chats``, листает страницы
      по 20, фильтрует ``is_chat_visible`` («работодатель ответил, не
      отказом») и апсертит в ``chats`` / ``messages``.
    * **habr** — ``app.services.job_sites.habr.chats_sync.sync_habr_chats_for_user``
      листает HTML ``career.habr.com/conversations`` (читает
      гидрационный payload ``__NUXT_DATA__`` через devalue-парсер),
      апсертит то же.

    Возвращает агрегированные счётчики:

        {
            "ok": true,
            "seen":   <всего пришло из API>,
            "saved":  <апсерт-нуто строк>,
            "per_service": {
                "hh":   {"ok": ..., "seen": ..., "saved": ...},
                "habr": {"ok": ..., "seen": ..., "saved": ...}
            },
            "chats": [...]   # снимок левой панели для фронта
        }

    HTTP-ошибки:
        * 400 ``no_platforms`` — у юзера нет ни одной активной площадки.

    Ошибки внутри каждого sync (auth expired, network) НЕ валят запрос:
    они оседают в ``per_service[<slug>].ok=false`` + ``error``/``message``,
    UI показывает баннер на той площадке, где сломалось. Остальные
    апдейтятся как ни в чём не бывало.

    Эндпойнт идемпотентный.
    """
    import asyncio

    user = await _require_user(request, db)

    # Определяем, какие площадки подключены. Активная = есть строка
    # в ``platform_credentials`` со ``status='active'``. Это тот же
    # источник истины, что и /settings, и воркер очереди.
    active_platforms: set[str] = set(
        (
            await db.execute(
                select(PlatformCredential.platform).where(
                    PlatformCredential.user_id == user.id,
                    PlatformCredential.status == PC_STATUS_ACTIVE,
                )
            )
        ).scalars().all()
    )

    # hh.ru идёт ещё и через ``hh_phone`` (per-user lock и SMS-перелогин),
    # поэтому считаем подключённой только если есть и креды, и телефон.
    hh_phone = request.session.get("hh_phone") or user.phone_number
    habr_email = _habr_email_for(user)

    tasks: dict[str, Any] = {}
    if PLATFORM_HH in active_platforms and hh_phone:
        from app.services.job_sites.hh.chats_sync import (
            sync_hh_chats_for_user,
        )

        tasks[PLATFORM_HH] = sync_hh_chats_for_user(
            user_id=user.id, hh_phone=hh_phone,
        )
    if PLATFORM_HABR in active_platforms:
        from app.services.job_sites.habr.chats_sync import (
            sync_habr_chats_for_user,
        )

        tasks[PLATFORM_HABR] = sync_habr_chats_for_user(
            user_id=user.id, habr_email=habr_email or "",
        )

    if not tasks:
        # Нет ни одной площадки, с которой имеет смысл синкаться.
        # Это не серверная ошибка — просто фронт зря дёрнул. Отдаём
        # 400, чтобы UI показал «подключите площадку в /settings».
        raise HTTPException(
            status_code=400,
            detail={
                "error": "no_platforms",
                "message": (
                    "Не подключена ни одна площадка для чатов. "
                    "Войдите в hh.ru или habr через /settings."
                ),
            },
        )

    # ``return_exceptions=True`` — чтобы падение одного sync'а не валило
    # все остальные. Каждую ошибку разворачиваем индивидуально.
    slugs = list(tasks.keys())
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    from app.services.job_sites.habr import HabrAuthError
    from app.services.job_sites.hh import HHAuthError

    per_service: dict[str, dict[str, Any]] = {}
    total_seen = 0
    total_saved = 0
    for slug, result in zip(slugs, results):
        if isinstance(result, HHAuthError):
            per_service[slug] = {
                "ok": False,
                "error": "hh_session_expired",
                "message": "Сессия hh.ru истекла. Войдите заново.",
            }
            continue
        if isinstance(result, HabrAuthError):
            per_service[slug] = {
                "ok": False,
                "error": "habr_session_expired",
                "message": "Сессия habr истекла. Войдите заново.",
            }
            continue
        if isinstance(result, Exception):
            logger.exception(
                "chats_sync %s failed for user=%s", slug, user.id,
                exc_info=result,
            )
            per_service[slug] = {
                "ok": False,
                "error": "sync_failed",
                "message": str(result) or result.__class__.__name__,
            }
            continue
        stats = result if isinstance(result, dict) else {}
        per_service[slug] = {"ok": True, **stats}
        total_seen += int(stats.get("seen") or 0)
        total_saved += int(stats.get("saved") or 0)

    # Снимок левой панели — на нём фронт перерисовывает список чатов,
    # без полного reload страницы. ``chat_counts`` дают цифры рядом
    # с фильтрами «Все чаты / Актуальные / Отказы / В ожидании» —
    # без них после фонового sync счётчики застывали на SSR-значениях.
    chats_snapshot = await list_chat_cards(db, user_id=user.id)
    chat_counts = await chats_status_counts(db, user_id=user.id)
    # NOTIFY уйдёт после commit'а в ``get_db``, и WebSocket-подписчики
    # (включая этого же юзера на других вкладках) получат push.
    if total_saved:
        await notify_chat_change(db, user.id)
    return {
        "ok": True,
        "seen": total_seen,
        "saved": total_saved,
        "per_service": per_service,
        "chats": chats_snapshot,
        "chat_counts": chat_counts,
    }


def _hh_phone_for(user: User, request: Request) -> str | None:
    """Извлечь hh-телефон: приоритет — сессия, fallback — БД (`user.phone_number`)."""
    return request.session.get("hh_phone") or user.phone_number


def _habr_email_for(user: User) -> str | None:
    """Извлечь habr-логин: основной ``user.email``, fallback — ``user.temp_email``.

    Хабр-аккаунт юзера живёт под основным email (``user.email`` после
    ``confirm_email_change``), но если основной ещё не подтверждён —
    он на ``user.temp_email`` (выдаётся через ``temp.coda.ink``).
    Cookies сессии всё равно лежат в
    ``platform_credentials.encrypted_session`` по ``user_id``, так что
    email тут нужен в первую очередь для OTP-бриджа и автоматического
    перелогина (см. ``HabrClient._load_habr_login_credentials``).
    """
    return user.email or user.temp_email


async def _chat_by_uuid(
    db: AsyncSession, *, user_id: uuid.UUID, chat_uuid_raw: str
) -> Chat:
    """Загрузить ``Chat`` по UUID и проверить, что он принадлежит юзеру."""
    try:
        chat_uuid = uuid.UUID(chat_uuid_raw)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail={"error": "chat_not_found"})
    chat = (
        await db.execute(
            select(Chat).where(Chat.id == chat_uuid, Chat.user_id == user_id)
        )
    ).scalar_one_or_none()
    if chat is None:
        raise HTTPException(status_code=404, detail={"error": "chat_not_found"})
    return chat


@router.get("/chats/{chat_uuid}")
async def chats_get_one(
    chat_uuid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """JSON-описание активного диалога — быстрый путь (только из БД).

    Шаги:
    1. ``_mark_chat_read_inline`` — гасит непрочитанные в БД сразу.
    2. ``build_active_chat_ctx`` без ``fetch_remote`` — собираем шапку
       и сообщения из локальной БД, без похода на hh.ru. Это занимает
       ~50ms.

    Свежие сообщения подтягивает отдельный ``POST .../refresh`` —
    фронт дёргает его параллельно и обновляет DOM, когда придут
    свежие данные.
    """
    from app.db.dashboard_data import _mark_chat_read_inline

    user = await _require_user(request, db)
    chat = await _chat_by_uuid(db, user_id=user.id, chat_uuid_raw=chat_uuid)

    await _mark_chat_read_inline(db, user_id=user.id, chat_uuid=chat.id)
    await db.flush()

    active_chat = await build_active_chat_ctx(
        db, user=user, chat=chat, hh_phone=None,
    )
    await db.commit()

    return {"ok": True, "active_chat": active_chat}


@router.post("/chats/{chat_uuid}/refresh")
async def chats_refresh_history(
    chat_uuid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Догрузить полную историю диалога с площадки и вернуть актуальный
    ``active_chat``.

    Эндпойнт намеренно отделён от ``GET /api/chats/<id>``, чтобы тяжёлый
    сетевой поход за свежими сообщениями не тормозил основной рендер
    диалога (~1-2с roundtrip vs ~50ms на «быстрый» путь). Фронт
    вызывает его в фоне после первого рендера и просто перерисовывает
    DOM, если что-то добавилось.

    Куда идём — определяется ``chat.service``:

    * ``hh``   → ``chatik.hh.ru/chatik/api/chat_data`` (через
      ``sync_hh_chat_messages_for_user``).
    * ``habr`` → ``career.habr.com/api/frontend_v1/chat/messages?login=…``
      (через ``sync_habr_chat_messages_for_user``).

    Сетевые ошибки не пробрасываются наверх — отдаём то, что в БД.
    """
    user = await _require_user(request, db)
    chat = await _chat_by_uuid(db, user_id=user.id, chat_uuid_raw=chat_uuid)

    hh_phone = _hh_phone_for(user, request)
    habr_email = _habr_email_for(user)
    active_chat = await build_active_chat_ctx(
        db, user=user, chat=chat,
        hh_phone=hh_phone, habr_email=habr_email,
        fetch_remote=True,
    )
    await db.commit()

    return {"ok": True, "active_chat": active_chat}


@router.post("/chats/{chat_uuid}/read")
async def chats_mark_read(
    chat_uuid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Пометить чат прочитанным.

    Делает две вещи:
    1. Сбрасывает в БД ``chats.unread_messages = 0`` и помечает
       сообщения этого чата ``is_read=true`` (best-effort).
    2. Шлёт POST на ``chatik.hh.ru/chatik/api/mark_read`` — чтобы и в
       hh-интерфейсе чат стал прочитан. ``messageId`` берём из
       ``chats.last_viewed_message_id`` (это
       ``lastViewedByCurrentUserMessageId`` из payload'а API чатов);
       fallback — ``Message.external_id`` последнего сообщения чата.

    Возвращает ``{ok: true, hh_marked: <bool>}``.

    HTTP-ошибки:
        * 400 ``no_hh_session`` — нет hh-телефона у юзера;
        * 401 ``hh_session_expired`` — cookies hh.ru протухли (БД-сброс всё
          равно проходит — флаг ``hh_marked=false``);
        * 404 ``chat_not_found`` — чужой чат или нет такого uuid.
    """
    user = await _require_user(request, db)
    chat = await _chat_by_uuid(db, user_id=user.id, chat_uuid_raw=chat_uuid)

    # 1) Локальный сброс — делаем всегда, даже если поход в hh.ru
    #    не получился. Юзер увидит «прочитано» сразу, hh-сторона
    #    подтянется при следующем синке.
    #
    #    Параллельно двигаем ``last_viewed_message_id`` на максимум:
    #    приоритет — id последнего сообщения чата по нашей БД (то, что
    #    юзер прямо сейчас прочитал в UI). Это нужно, чтобы ближайший
    #    sync с hh.ru (где сам mark_read может ещё не пропечататься)
    #    не воскресил счётчик «непрочитанных» — см.
    #    ``_upsert_chat_and_last_message`` в ``chats_sync``.
    chat.unread_messages = 0
    last_msg_ext = (
        await db.execute(
            select(Message.external_id)
            .where(Message.chat_id == chat.id, Message.external_id.is_not(None))
            .order_by(Message.sent_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if last_msg_ext is not None:
        try:
            cur_lv = int(chat.last_viewed_message_id) if chat.last_viewed_message_id else None
            new_lv = int(last_msg_ext)
        except (TypeError, ValueError):
            cur_lv, new_lv = None, None
        if cur_lv is None or (new_lv is not None and new_lv > cur_lv):
            chat.last_viewed_message_id = last_msg_ext
    await db.execute(
        Message.__table__.update()
        .where(Message.chat_id == chat.id, Message.is_read.is_(False))
        .values(is_read=True)
    )
    # NOTIFY доедет до подписчиков после ``commit()`` ниже —
    # бейдж «непрочитанных» в левой панели слетит у всех вкладок.
    await notify_chat_change(db, user.id)
    await db.commit()

    # 2) Идём на площадку. Если креды не привязаны или сессия истекла —
    #    возвращаем 200 с ``remote_marked=false``, локально-то уже прочитано.
    if chat.service == SERVICE_HH:
        hh_phone = _hh_phone_for(user, request)
        if not hh_phone:
            return {
                "ok": True, "hh_marked": False,
                "remote_marked": False, "reason": "no_hh_session",
            }

        # messageId для mark_read: приоритет — last_viewed_message_id
        # (мы только что выставили его на самое свежее), fallback —
        # max(Message.external_id) этого чата.
        message_id = chat.last_viewed_message_id or last_msg_ext
        if not message_id:
            return {
                "ok": True, "hh_marked": False,
                "remote_marked": False, "reason": "no_message_id",
            }

        from app.services.job_sites.hh import HHAuthError
        from app.services.job_sites.hh.chats_sync import (
            REJECTED_APPLICANT_STATES,
            mark_hh_chat_read_for_user,
        )

        try:
            await mark_hh_chat_read_for_user(
                user_id=user.id,
                hh_phone=hh_phone,
                chat_external_id=chat.external_id,
                message_id=str(message_id),
                has_unread_discard=(
                    chat.applicant_state in REJECTED_APPLICANT_STATES
                ),
            )
        except HHAuthError:
            return {
                "ok": True, "hh_marked": False,
                "remote_marked": False, "reason": "hh_session_expired",
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "mark_chat_read failed: user=%s chat=%s", user.id, chat.id
            )
            return {
                "ok": True, "hh_marked": False, "remote_marked": False,
                "reason": "hh_error",
                "message": str(exc) or exc.__class__.__name__,
            }

        return {"ok": True, "hh_marked": True, "remote_marked": True}

    if chat.service == SERVICE_HABR:
        habr_email = _habr_email_for(user)
        from app.services.job_sites.habr import HabrAuthError
        from app.services.job_sites.habr.chats_sync import (
            mark_habr_chat_read_for_user,
        )

        try:
            await mark_habr_chat_read_for_user(
                user_id=user.id,
                habr_email=habr_email or "",
                chat_external_id=chat.external_id,
            )
        except HabrAuthError:
            return {
                "ok": True, "remote_marked": False,
                "reason": "habr_session_expired",
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "mark_chat_read failed: user=%s chat=%s", user.id, chat.id
            )
            return {
                "ok": True, "remote_marked": False,
                "reason": "habr_error",
                "message": str(exc) or exc.__class__.__name__,
            }

        return {"ok": True, "remote_marked": True}

    # Незнакомый сервис — локально уже прочитано, удалённо ничего не
    # делаем (LinkedIn / etc ещё не имплементированы).
    return {"ok": True, "remote_marked": False, "reason": "unsupported_service"}


@router.post("/chats/{chat_uuid}/messages")
async def chats_send_message(
    chat_uuid: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Отправить сообщение в чат на стороне площадки.

    Body: ``{"text": "<сообщение>"}``. Сейчас работает только для
    ``service=hh`` (другие площадки добавятся отдельно).

    Возвращает сохранённое локальное сообщение (для оптимистичной
    отрисовки на фронте) + поле ``hh_message_id`` если hh подтвердил.
    """
    user = await _require_user(request, db)
    chat = await _chat_by_uuid(db, user_id=user.id, chat_uuid_raw=chat_uuid)

    body = await request.json()
    text = (body or {}).get("text") or ""
    text = text.strip()
    if not text:
        raise HTTPException(
            status_code=400,
            detail={"error": "empty_text", "message": "Пустое сообщение."},
        )
    if len(text) > 4000:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "text_too_long",
                "message": "Сообщение длиннее 4000 символов.",
            },
        )

    if chat.service == SERVICE_HH:
        hh_phone = _hh_phone_for(user, request)
        if not hh_phone:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "no_hh_session",
                    "message": (
                        "Нет привязки к hh.ru. Войдите в аккаунт hh.ru через "
                        "/settings."
                    ),
                },
            )

        from app.services.job_sites.hh import HHAuthError
        from app.services.job_sites.hh.chats_sync import (
            send_hh_chat_message_for_user,
        )

        try:
            hh_response = await send_hh_chat_message_for_user(
                user_id=user.id,
                hh_phone=hh_phone,
                chat_external_id=chat.external_id,
                text=text,
            )
        except HHAuthError:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "hh_session_expired",
                    "message": "Сессия hh.ru истекла. Войдите заново.",
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "send_chat_message failed: user=%s chat=%s", user.id, chat.id
            )
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "hh_send_failed",
                    "message": str(exc) or exc.__class__.__name__,
                },
            )

        # hh возвращает либо {"message": {...}} либо просто {...}. Достаём id.
        hh_msg = (
            hh_response.get("message") if isinstance(hh_response, dict) else None
        )
        if not isinstance(hh_msg, dict):
            hh_msg = hh_response if isinstance(hh_response, dict) else {}
        remote_message_id = hh_msg.get("id")

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        new_msg = Message(
            chat_id=chat.id,
            external_id=(
                str(remote_message_id) if remote_message_id is not None else None
            ),
            text=text,
            author="me",
            author_name="Я",
            is_read=True,
            sent_at=now,
        )
        db.add(new_msg)
        chat.last_message_preview = text[:300]
        chat.last_activity_at = now
        chat.last_message_outgoing = True
        await notify_chat_change(db, user.id)
        await db.commit()
        await db.refresh(new_msg)

        return {
            "ok": True,
            "hh_message_id": remote_message_id,
            "remote_message_id": remote_message_id,
            "message": {
                "id": str(new_msg.id),
                "text": new_msg.text,
                "sent_at": new_msg.sent_at.isoformat(),
                "outgoing": True,
            },
        }

    if chat.service == SERVICE_HABR:
        habr_email = _habr_email_for(user)
        from app.services.job_sites.habr import HabrAuthError
        from app.services.job_sites.habr.chats_sync import (
            send_habr_chat_message_for_user,
            sync_habr_chat_messages_for_user,
        )

        try:
            habr_response, current_user_alias = await send_habr_chat_message_for_user(
                user_id=user.id,
                habr_email=habr_email or "",
                chat_external_id=chat.external_id,
                text=text,
            )
        except HabrAuthError:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "habr_session_expired",
                    "message": "Сессия habr истекла. Войдите заново.",
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "send_chat_message failed: user=%s chat=%s", user.id, chat.id
            )
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "habr_send_failed",
                    "message": str(exc) or exc.__class__.__name__,
                },
            )

        # habr на POST /chat/messages возвращает само сообщение в плоском
        # виде ({"id": ..., "body": "...", "createdAt": "...", ...}) — без
        # обёртки ``message``. Достаём id для optimistic UI.
        habr_msg = habr_response if isinstance(habr_response, dict) else {}
        remote_message_id = habr_msg.get("id")

        # Подтягиваем свежий хвост сообщений (включая только что
        # отправленное) — упрощает синхронизацию с тем, что покажет
        # habr-UI: парсер тогда увидит реальные id/createdAt без
        # дублирования с локальной optimistic-вставкой.
        try:
            await sync_habr_chat_messages_for_user(
                db, user_id=user.id, chat=chat, habr_email=habr_email or "",
            )
        except Exception:  # noqa: BLE001
            # Логирование — внутри sync; здесь не валим отправку.
            pass

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        # Если sync_habr_chat_messages_for_user уже вставил это сообщение
        # по external_id — повторно его не добавляем, чтобы не было
        # дубля. В optimistic-ответе всё равно отдаём текст/время.
        already_inserted = False
        if remote_message_id is not None:
            existing = (
                await db.execute(
                    select(Message).where(
                        Message.chat_id == chat.id,
                        Message.external_id == str(remote_message_id),
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                already_inserted = True
                new_msg = existing

        if not already_inserted:
            new_msg = Message(
                chat_id=chat.id,
                external_id=(
                    str(remote_message_id) if remote_message_id is not None else None
                ),
                text=text,
                author="me",
                author_name=(
                    current_user_alias if current_user_alias else "Я"
                ),
                is_read=True,
                sent_at=now,
            )
            db.add(new_msg)
            chat.last_message_preview = text[:300]
            chat.last_activity_at = now
            chat.last_message_outgoing = True
            await notify_chat_change(db, user.id)
            await db.commit()
            await db.refresh(new_msg)
        else:
            await notify_chat_change(db, user.id)
            await db.commit()

        return {
            "ok": True,
            "remote_message_id": remote_message_id,
            "message": {
                "id": str(new_msg.id),
                "text": new_msg.text,
                "sent_at": new_msg.sent_at.isoformat(),
                "outgoing": True,
            },
        }

    raise HTTPException(
        status_code=400,
        detail={
            "error": "unsupported_service",
            "message": (
                "Отправка сообщений для этой площадки пока не поддерживается."
            ),
        },
    )
