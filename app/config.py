"""Application configuration. Values are read from environment variables.

Used by:
- ``app.api.auth`` (TWOCAPTCHA_KEY)
- ``app.main`` (SESSION_SECRET, debug flags)
- ``app.services.job_sites.base`` (SESSION_SECRET → Fernet ключ для
  ``platform_credentials.encrypted_session``)
- ``app.db.session`` (via :func:`get_settings`)
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# ── Paths ───────────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env", override=False)

# ── Secrets ─────────────────────────────────────────────────────────────────
# 2captcha API key — required to actually solve hh.ru captcha.
# Without it /auth/request-code returns 500 twocaptcha_key_not_configured.
TWOCAPTCHA_KEY: str = os.getenv("TWOCAPTCHA_KEY", "")
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

# temp.coda.ink — сервис временных почтовых ящиков.
# ``TEMP_MAIL_API_TOKEN`` — API-ключ (формат ``tm_…``), выдаётся
# владельцем сервиса вручную (см. https://temp.coda.ink/api-docs).
# Он используется только при СОЗДАНИИ ящика (``POST /v1/address``):
# адреса, созданные с этим ключом, не имеют автоэкспайра и подпадают
# под повышенные лимиты (20 created/min vs 3/min, 25 active vs 3).
# Для чтения inbox'а сервис всё равно требует per-address token
# (``tm_at_…``) — мы сохраняем его в БД (``users.temp_email_token``),
# чтобы доступ к ящику оставался у нас постоянно.
TEMP_MAIL_API_TOKEN: str = os.getenv("TEMP_MAIL_API_TOKEN", "")
TEMP_MAIL_BASE_URL: str = os.getenv(
    "TEMP_MAIL_BASE_URL",
    "https://temp.coda.ink/v1",
)

# Server-side session cookie signing secret. CHANGE THIS in production.
SESSION_SECRET: str = os.getenv(
    "SESSION_SECRET",
    "dev-only-secret-change-me-in-production-please",
)

# Соль для детерминированной генерации «платформенного» пароля,
# который мы показываем пользователю в /settings и используем при
# регистрации/восстановлении его аккаунтов на job-площадках.
# Пароль = производная (sha256) от user.id + этой соли — то есть
# в БД пароль НЕ хранится, его всегда можно пересчитать по
# ``users.id``. Поэтому ротация соли == ротация всех «платформенных»
# паролей сразу; на проде задавай через .env и не меняй без
# понимания последствий.
PLATFORM_PASSWORD_SALT: str = os.getenv(
    "PLATFORM_PASSWORD_SALT",
    "dev-only-platform-pw-salt-change-me",
)

# ── Misc ────────────────────────────────────────────────────────────────────
DEBUG: bool = os.getenv("OFFERDAY_DEBUG", "0") in ("1", "true", "True")


# ── Database ────────────────────────────────────────────────────────────────
# Async PostgreSQL is the only supported database.  The app expects an
# asyncpg-flavoured DSN — see .env.example and docker-compose.yml.
#
# The default value here matches the docker-compose Postgres service so
# `docker compose up -d db && uvicorn app.main:app` just works locally.
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://offerday:offerday@localhost:5432/offerday",
)

APP_ENV: str = os.getenv("APP_ENV", "development")


def _validate_database_url(url: str) -> str:
    """Sanity-check that ``DATABASE_URL`` is an async Postgres DSN.

    The project intentionally ships a single driver (asyncpg).  Bailing
    out at startup is much friendlier than the ``MissingGreenlet`` /
    ``InvalidRequestError`` SQLAlchemy throws on the first query when
    a sync DSN slips into an async engine.
    """
    if not url.startswith(("postgresql+asyncpg://", "postgresql+asyncpg:")):
        raise RuntimeError(
            "DATABASE_URL must be an async PostgreSQL DSN, e.g. "
            "postgresql+asyncpg://user:pass@host:5432/db. "
            f"Got: {url!r}"
        )
    return url


@dataclass(frozen=True)
class Settings:
    """Settings object consumed by :mod:`app.db.session`.

    Kept tiny on purpose — every field is something that module reads.
    """

    database_url: str
    app_env: str
    app_debug: bool


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings(
        database_url=_validate_database_url(DATABASE_URL),
        app_env=APP_ENV,
        app_debug=DEBUG,
    )
