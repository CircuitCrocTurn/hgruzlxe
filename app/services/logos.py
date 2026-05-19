"""Скачивание логотипов компаний в ``app/static/logotips/``.

Воркер ``work_at_all`` зовёт :func:`download_company_logo` сразу
после парсинга вакансии. Картинка кладётся под именем
``<vacancy_response_id>.<ext>`` (id — UUID будущей строки
``vacancy_responses``). Это единственный публичный путь до картинки —
шаблон ``responses.html`` собирает ссылку как
``/static/logotips/<id>.<logo_ext>``.

Дизайн-решения:

* **Имя файла = UUID отклика.** Не URL, не company-id с площадки.
  Преимущества: имя гарантированно уникально, файл не «теряется»,
  если у компании поменяли логотип. Минус — у одной компании могут
  быть несколько файлов (по одному на каждый отклик). Это
  осознанная плата за простоту; общий объём всё равно копеечный.
* **Расширение запоминается в БД** (``vacancy_responses.logo_ext``).
  Иначе шаблону пришлось бы перебирать ``glob("<id>.*")`` каждый
  раз — лишний I/O.
* **Sync timeout + try/except.** Любая ошибка скачивания мягкая:
  карточка просто покажет инициалы. Сетевые сбои на площадке
  не должны валить весь воркер.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from app.config import PROJECT_ROOT

logger = logging.getLogger("offerday.services.logos")

# Где лежат картинки. Совпадает с тем, что мы отдаём через
# ``app.mount("/static", ...)`` (см. ``app/main.py``).
_LOGOTIPS_DIR: Path = PROJECT_ROOT / "app" / "static" / "logotips"

# Расширения, которые мы готовы принять. Браузер сам поймёт, что
# отдавать (через ``Content-Type`` от StaticFiles), но мы хотим, чтобы
# на диске лежал «правильный» файл — без подмены jpg на png.
_ALLOWED_EXTENSIONS: frozenset[str] = frozenset({"png", "jpg", "jpeg", "svg", "webp", "gif"})

# Размер «капа» — отрезаем явный треш (например, кто-то отдал HTML
# вместо картинки). 5 МБ — с большим запасом, реальные логотипы
# обычно <50 КБ.
_MAX_LOGO_BYTES: int = 5 * 1024 * 1024


def _ext_from_url(url: str) -> str:
    """Достаём расширение из URL, например ``"png"``.

    Если расширения нет / оно не из ``_ALLOWED_EXTENSIONS``, возвращаем
    ``"png"`` как безопасный дефолт (браузеры всё равно посмотрят
    Content-Type от ``StaticFiles``).
    """
    parsed = urlparse(url)
    path = parsed.path or ""
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in _ALLOWED_EXTENSIONS:
        # Нормализуем jpeg → jpg, чтобы в БД всегда был «короткий» вариант.
        return "jpg" if ext == "jpeg" else ext
    return "png"


def _ensure_logotips_dir() -> None:
    _LOGOTIPS_DIR.mkdir(parents=True, exist_ok=True)


async def download_company_logo(
    url: str,
    vacancy_response_id: uuid.UUID,
    *,
    session: aiohttp.ClientSession | None = None,
    timeout_s: float = 10.0,
) -> str | None:
    """Скачивает картинку с ``url`` в ``app/static/logotips/<id>.<ext>``.

    Параметры
    ----------
    url
        Полный URL картинки (http/https). Пустая строка / относительный
        URL → ничего не делаем.
    vacancy_response_id
        UUID будущей (или уже созданной) строки ``vacancy_responses``.
        Используется как basename файла.
    session
        Опциональный ``aiohttp.ClientSession`` — если воркер уже
        держит открытую сессию (например, ``HHClient.session``), удобно
        переиспользовать её, иначе создадим временную.
    timeout_s
        Бюджет на скачивание. Дефолт 10 с — логотипы маленькие, дольше
        ждать не имеет смысла.

    Возвращает
    -----------
    ``str`` — расширение (``"png"``, ``"jpg"``, ...), которое нужно
    положить в колонку ``vacancy_responses.logo_ext``.
    ``None`` — если ничего не скачали (нет URL, сеть упала,
    нелепый content-type, и т.д.). Карточка отклика в этом случае
    отрисует инициалы вместо логотипа.
    """
    if not url:
        return None
    if not url.startswith(("http://", "https://")):
        # hh иногда отдаёт `/employer-logo/...` без схемы — у нас нет
        # надёжного способа угадать домен, поэтому пропускаем.
        logger.debug("logo url is not absolute, skipping: %s", url)
        return None

    _ensure_logotips_dir()
    ext = _ext_from_url(url)
    dest = _LOGOTIPS_DIR / f"{vacancy_response_id}.{ext}"

    own_session = session is None
    client = session or aiohttp.ClientSession()
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with client.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                logger.info(
                    "logo download non-200: status=%s url=%s", resp.status, url
                )
                return None
            content_type = (resp.headers.get("Content-Type") or "").lower()
            if content_type and not content_type.startswith("image/"):
                logger.info(
                    "logo download bad content-type: %s url=%s", content_type, url
                )
                return None
            data = await resp.read()
        if not data or len(data) > _MAX_LOGO_BYTES:
            logger.info("logo download empty/oversized: bytes=%s", len(data) if data else 0)
            return None
        dest.write_bytes(data)
        return ext
    except Exception as exc:
        # Сеть может моргнуть, сертификат может протухнуть — не валим
        # воркер из-за такой ерунды.
        logger.warning("logo download failed: %s (url=%s)", exc, url)
        return None
    finally:
        if own_session:
            await client.close()
