"""Offerday — FastAPI entrypoint.

Serves the landing page, the SMS-login page, and the four authenticated
dashboard pages (``/dashboard``, ``/responses``, ``/chats``,
``/settings``), and includes the ``/auth/*`` router that talks to hh.ru
via :class:`HHClient`.

Each authenticated page pulls its data from the database (see
:mod:`app.db.dashboard_data`).  When a section has no data the
templates render an empty state instead of placeholder mock-ups.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app.api.auth import router as auth_router
from app.api.dashboard import router as dashboard_api_router
from app.config import DEBUG, PROJECT_ROOT, SESSION_SECRET
from app.db import dashboard_data  # noqa: F401  — keeps the import side-effect-free hook
from app.db.session import engine, get_db
from app.db.users import ensure_user_for_session
from app.queue import reset_stuck_jobs
from app.queue.scheduler import start_scheduler, stop_scheduler
from app.services.job_sites.dispatcher import verify_consistency as verify_job_sites

logger = logging.getLogger("offerday")
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Отключаем логи HTTP-клиентов (OpenAI, httpx, httpcore)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

# (Опционально) сделать тише сам uvicorn
logging.getLogger("uvicorn.access").setLevel(logging.INFO)
logging.getLogger("uvicorn.error").setLevel(logging.INFO)   # ошибки оставляем

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan hook.

    Schema management lives in Alembic — run ``alembic upgrade head``
    before the first boot (and on every deploy).  We do NOT call
    ``Base.metadata.create_all`` here: on Postgres it would silently
    drift from the migration history and bite us on the next
    autogenerate.

    The hook is still useful for releasing the engine's connection
    pool cleanly on shutdown.
    """
    logger.info("offerday starting; database engine ready")
    # Текущий runner фоновых задач — in-process. Резюме парсятся
    # inline через ``asyncio.create_task`` (см. ``enqueue_resume_parse``),
    # а ``job_runs`` остаются единственной живой персистентной очередью
    # (для воркера ``work_at_all``). При рестарте все ``queued``/
    # ``running`` строки в ``job_runs`` помечаем ``failed``, чтобы
    # шедулер мог снова поставить задачу. Когда переедем на arq+Redis,
    # эту строчку можно будет убрать.
    try:
        await reset_stuck_jobs()
    except Exception:
        logger.exception("reset_stuck_jobs on startup failed")

    # Sanity check: для каждой площадки в каталоге UI должен быть
    # импортированный клиент (и наоборот). Лог покажет дисбаланс.
    try:
        verify_job_sites()
    except Exception:
        logger.exception("verify_job_sites on startup failed")

    # Периодический шедулер: раз в минуту обходит юзеров и кидает в
    # очередь ``work_at_all`` для тех, у кого настало время по тарифу.
    # Стартуем здесь, чтобы он жил весь срок процесса uvicorn'а;
    # ловим ошибки, чтобы upstream (uvicorn) поднялся даже если
    # планировщик не стартанёт по какой-то причине (например, БД
    # ещё не подняли).
    try:
        start_scheduler()
    except Exception:
        logger.exception("start_scheduler on startup failed")

    yield

    # Сначала тушим шедулер (чтобы он не успел натолкать новых задач
    # в очередь), потом закрываем engine.
    try:
        await stop_scheduler()
    except Exception:
        logger.exception("stop_scheduler on shutdown failed")

    await engine.dispose()


app = FastAPI(title="Offerday", debug=DEBUG, lifespan=lifespan)

# ── Middleware ──────────────────────────────────────────────────────────────
# auth.py reads/writes ``request.session`` — needs SessionMiddleware.
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="offerday_session",
    same_site="lax",
    https_only=False,  # set True behind HTTPS in prod
    max_age=14 * 24 * 60 * 60,  # 14 days
)

# ── Static files & templates ────────────────────────────────────────────────
app.mount(
    "/static",
    StaticFiles(directory=str(PROJECT_ROOT / "app" / "static")),
    name="static",
)
templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))


# ── Page routes ─────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    user = request.session.get("user")
    return templates.TemplateResponse(
        request, "landing.html", {"user": user},
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/dashboard", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


def _require_user(request: Request) -> dict[str, Any] | None:
    """Return the user dict from the session, or None if unauthenticated."""
    return request.session.get("user")


async def _ctx_for(
    request: Request,
    template_name: str,
    loader,
    db: AsyncSession,
    *,
    loader_kwargs: dict[str, Any] | None = None,
):
    """Run an authenticated page through the standard pipeline.

    1. Bounce unauthenticated requests to ``/login``.
    2. Look up the :class:`User` row by phone (no auto-create — see
       :func:`app.db.users.ensure_user_for_session`). Если в БД пусто
       (например, БД очистили, а кука осталась) — сбрасываем сессию и
       редиректим на ``/login``.
    3. Hand off to ``loader`` to build the template context.
    """
    session_user = _require_user(request)
    if not session_user:
        return RedirectResponse("/login", status_code=303)
    db_user = await ensure_user_for_session(db, session_user)
    if db_user is None:
        # Кука валидна, но юзера в БД нет. Чистим сессию, чтобы
        # следующий запрос не повторял этот же путь, и просим заново
        # пройти OTP — единственный способ создать ``users`` row.
        request.session.clear()
        return RedirectResponse("/login", status_code=303)
    ctx = await loader(db, db_user, session_user, **(loader_kwargs or {}))
    return templates.TemplateResponse(request, template_name, ctx)


@app.get("/me", response_class=HTMLResponse)
async def me_page(request: Request):
    # Kept for backwards compatibility; canonical landing for logged-in users
    # is /dashboard now.
    if not _require_user(request):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await _ctx_for(request, "dashboard.html", dashboard_data.load_dashboard, db)


@app.get("/responses", response_class=HTMLResponse)
async def responses_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await _ctx_for(request, "responses.html", dashboard_data.load_responses, db)


@app.get("/chats", response_class=HTMLResponse)
async def chats_page(
    request: Request,
    id: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    return await _ctx_for(
        request,
        "chats.html",
        dashboard_data.load_chats,
        db,
        loader_kwargs={"active_chat_id": id},
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await _ctx_for(request, "settings.html", dashboard_data.load_settings, db)


@app.get("/tariffs", response_class=HTMLResponse)
async def tariffs_page(request: Request, db: AsyncSession = Depends(get_db)):
    return await _ctx_for(request, "tariffs.html", dashboard_data.load_tariffs, db)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ── Routers ─────────────────────────────────────────────────────────────────
app.include_router(auth_router)
app.include_router(dashboard_api_router)
