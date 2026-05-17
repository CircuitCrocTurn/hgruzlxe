"""Парсер документов-резюме через Anthropic Claude API.

Принимает на вход содержимое файла + MIME-тип, скармливает Claude'у
с принудительным structured output под :class:`app.schemas.resume.Resume`
(через механизм ``tool_use``), возвращает уже валидную pydantic-модель.

Поддерживаемые форматы:

* ``application/pdf`` — нативный PDF-input Claude (без локального
  извлечения текста; модель сама видит и текст, и вёрстку, и
  изображения внутри PDF).
* ``image/png`` / ``image/jpeg`` / ``image/webp`` / ``image/gif`` —
  vision-input. Подойдёт, если юзер скинул скриншот резюме или PDF,
  конвертированный в картинку.
* ``application/vnd.openxmlformats-officedocument.wordprocessingml.document``
  (`.docx`) — извлекаем plain-text из ``word/document.xml`` через
  ``zipfile`` (без внешних зависимостей вроде ``python-docx``).
* ``application/msword`` (`.doc`) — старый бинарный формат. Без
  ``antiword`` / ``textract`` распарсить его в чистом Python тяжело.
  В этом случае пробуем «спасти» хоть что-то, скармливая байты как
  UTF-8 с ``errors="ignore"`` — модель неплохо вытягивает данные
  даже из такого «грязного» текста. Лучше юзерам всё-таки давать
  PDF / DOCX / изображения.
* ``text/plain`` (`.txt`) — просто читаем как UTF-8.

Использование::

    from app.services.resume_parser import parse_resume_file

    parsed: Resume = await parse_resume_file(Path("/tmp/resume.pdf"))
    # parsed.full_name, parsed.email, parsed.experience и т.д.

Внутри использует ``httpx`` (без зависимости от ``anthropic`` SDK,
который не входит в текущий requirements.txt). Модель задаётся через
переменную окружения ``ANTHROPIC_MODEL`` или константу ниже.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import mimetypes
import os
import re
import zipfile
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from app.schemas.resume import Resume as ResumeSchema
from app.config import ANTHROPIC_API_KEY

logger = logging.getLogger("offerday.services.resume_parser")


# ── Конфиг ──────────────────────────────────────────────────────────


_ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_API_VERSION = "2023-06-01"

#: Имя модели можно переопределить через ``ANTHROPIC_MODEL``. По
#: умолчанию — Opus 4.1 (как самая аккуратная по качеству на момент
#: написания). Если у Anthropic появится более новая Opus, достаточно
#: проставить переменную окружения, перезапуска парсера достаточно.
_DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-7")

#: Лимит сетевого таймаута на один запрос. Парсинг PDF на 4-5
#: страниц у Claude занимает обычно 10-25 секунд, плюс TLS-overhead.
_HTTP_TIMEOUT = httpx.Timeout(connect=15.0, read=180.0, write=60.0, pool=15.0)

#: Сколько токенов разрешаем сгенерировать модели в одном вызове.
#: tool_use-ответ — это структурированный JSON, обычно умещается в
#: 4000-6000 токенов даже на длинном резюме.
_MAX_TOKENS = 8000

#: MIME-типы, для которых используем нативную PDF-аппликацию.
_PDF_MIMES = {"application/pdf"}
#: MIME-типы, для которых используем vision-блок.
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}
#: docx (Office Open XML).
_DOCX_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
#: Старый .doc (бинарный) — best-effort.
_DOC_MIMES = {"application/msword"}
#: Plain-text.
_TXT_MIMES = {"text/plain"}


# ── Промпт ──────────────────────────────────────────────────────────


_SYSTEM_PROMPT = (
    "Ты — парсер резюме. На вход тебе придёт документ (PDF, изображение, "
    "DOCX или plain-text), содержащий резюме соискателя. Твоя задача — "
    "извлечь из него структурированную информацию и вызвать инструмент "
    "`save_resume` с заполненными полями.\n\n"
    "ЖЁСТКИЕ ПРАВИЛА:\n"
    "1) Никаких догадок. Если поле не указано в резюме явно — оставляй "
    "его null или пустым массивом. Не выдумывай данные.\n"
    "2) Сохраняй формулировки соискателя как есть (например, для дат — "
    "«Декабрь 2022», для уровня языка — «B2 — средне-продвинутый»). "
    "Это нужно для последующей сверки с оригиналом.\n"
    "3) `email` — если в резюме есть несколько email'ов, бери первый "
    "по порядку упоминания.\n"
    "4) `phone` — предпочитаемый телефон, помеченный соискателем как "
    "«предпочитаемый способ связи». Если такой пометки нет — первый "
    "телефон по порядку.\n"
    "5) Все контакты (телефоны, email'ы, мессенджеры, ссылки на "
    "соцсети) клади в `contacts[]` с указанием `kind` "
    "(phone / email / messenger / link / other).\n"
    "6) `skills[]` — массив отдельных навыков, без многословных "
    "описаний. Каждый элемент — короткая строка (\"Python\", "
    "\"PostgreSQL\", \"FastAPI\", \"Kafka\").\n"
    "7) Опыт работы (`experience[]`) — каждое место работы отдельным "
    "объектом, в хронологическом порядке от свежего к старому.\n"
    "8) Если резюме на нескольких языках или содержит явный перевод — "
    "извлекай данные на том языке, на котором они написаны в оригинале."
)


# ── JSON-схема инструмента save_resume ──────────────────────────────


def _build_tool_schema() -> dict[str, Any]:
    """Сгенерировать JSON-схему инструмента ``save_resume`` из
    pydantic-модели :class:`ResumeSchema`.

    Anthropic API принимает JSON Schema напрямую как ``input_schema``
    тула; pydantic v2 уже умеет генерить ровно ту структуру, которая
    нам нужна. Дополнительно проставляем ``additionalProperties:
    false`` на верхнем уровне, чтобы модель не клала сторонние поля.
    """
    schema = ResumeSchema.model_json_schema(mode="serialization")
    # На верхнем уровне Anthropic ожидает плоский ``type: "object"`` с
    # ``properties``. Pydantic ровно это и отдаёт; но иногда
    # ``additionalProperties`` отсутствует — добавим явно.
    if isinstance(schema, dict) and schema.get("type") == "object":
        schema.setdefault("additionalProperties", False)
    return schema


_TOOL_DESCRIPTION = (
    "Сохранить извлечённые из резюме данные. Ровно одна такая команда "
    "за один документ. Поля, которых нет в резюме, не заполняй — "
    "оставь null / пустой массив."
)


def _build_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "save_resume",
            "description": _TOOL_DESCRIPTION,
            "input_schema": _build_tool_schema(),
        }
    ]


# ── Ошибки ──────────────────────────────────────────────────────────


class ResumeParseError(RuntimeError):
    """Любая нежелательная ситуация при парсинге документа.

    Используется как «зонтичная» ошибка: вызывающему коду удобно
    ловить именно её и переводить в HTTP 4xx/5xx или в статус
    фоновой задачи.
    """


# ── Извлечение текста из локальных форматов ────────────────────────


_DOCX_T_RE = re.compile(r"<w:t[^>]*>(.*?)</w:t>", flags=re.DOTALL)
_DOCX_BR_RE = re.compile(r"<w:(br|cr)\b[^>]*/?>")
_DOCX_P_END_RE = re.compile(r"</w:p>")


def _docx_to_text(blob: bytes) -> str:
    """Извлекает plain-text из ``.docx`` (Office Open XML) без
    внешних зависимостей.

    Простая, но достаточная для резюме реализация: распаковываем zip,
    читаем ``word/document.xml``, заменяем ``<w:br/>`` и ``</w:p>``
    на перевод строки, остальные теги вырезаем регулярками.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            try:
                xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
            except KeyError as exc:
                raise ResumeParseError(
                    "docx_missing_document_xml"
                ) from exc
    except zipfile.BadZipFile as exc:
        raise ResumeParseError("docx_bad_zip") from exc

    # Сначала склеим текст из всех ``<w:t>`` и расставим переводы строк.
    xml = _DOCX_BR_RE.sub("\n", xml)
    xml = _DOCX_P_END_RE.sub("\n", xml)
    parts = _DOCX_T_RE.findall(xml)
    text = "\n".join(p.strip() for p in parts if p.strip())
    # Сжать слишком большие пустые промежутки.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


# ── Сборка content-блока для Anthropic ─────────────────────────────


def _normalize_mime(mime_type: str | None, *, filename: str | None = None) -> str:
    """Привести MIME-тип к одному из ожидаемых.

    Если ``mime_type`` пуст / нестандартен, пытаемся угадать по
    расширению файла через ``mimetypes.guess_type``.
    """
    mt = (mime_type or "").strip().lower()
    if not mt and filename:
        guessed, _ = mimetypes.guess_type(filename)
        mt = (guessed or "").lower()
    if mt == "image/jpg":
        mt = "image/jpeg"
    return mt


def _build_user_content(
    content_bytes: bytes,
    *,
    mime_type: str,
    filename: str | None,
) -> list[dict[str, Any]]:
    """Собрать массив content-блоков под формат Anthropic Messages API.

    Возвращает уже готовый список, который кладётся в
    ``messages[0].content``. Помимо самого документа добавляет
    короткое текстовое указание, что нужно вызвать инструмент.
    """
    mt = _normalize_mime(mime_type, filename=filename)
    instruction = (
        "Извлеки структурированные данные из приложенного резюме и "
        "вызови инструмент `save_resume`. Никаких лишних объяснений в "
        "ответе быть не должно — только вызов инструмента."
    )

    if mt in _PDF_MIMES:
        return [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(content_bytes).decode("ascii"),
                },
            },
            {"type": "text", "text": instruction},
        ]

    if mt in _IMAGE_MIMES:
        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mt,
                    "data": base64.b64encode(content_bytes).decode("ascii"),
                },
            },
            {"type": "text", "text": instruction},
        ]

    if mt in _DOCX_MIMES:
        text = _docx_to_text(content_bytes)
        return _wrap_text_content(text, instruction)

    if mt in _DOC_MIMES:
        # «Грязное» извлечение из бинарника .doc — оставляем как есть.
        text = content_bytes.decode("utf-8", errors="ignore")
        text = re.sub(r"[\x00-\x08\x0b-\x1f]+", " ", text)
        return _wrap_text_content(text, instruction)

    if mt in _TXT_MIMES or mt.startswith("text/"):
        text = content_bytes.decode("utf-8", errors="replace")
        return _wrap_text_content(text, instruction)

    raise ResumeParseError(f"unsupported_mime_type:{mt or 'unknown'}")


def _wrap_text_content(text: str, instruction: str) -> list[dict[str, Any]]:
    """Обёртка вокруг plain-text: на input даём один большой блок
    с самим резюме + отдельный — с инструкцией."""
    text = (text or "").strip()
    if not text:
        raise ResumeParseError("empty_document")
    return [
        {"type": "text", "text": f"Содержимое резюме:\n\n{text}"},
        {"type": "text", "text": instruction},
    ]


# ── Сетевой вызов ──────────────────────────────────────────────────


async def _call_anthropic(
    *,
    content_blocks: list[dict[str, Any]],
    api_key: str,
    model: str,
) -> dict[str, Any]:
    """Один POST на ``/v1/messages`` с форсированным tool_use.

    Возвращает уже распарсенный JSON-ответ. На любые HTTP-ошибки
    кидает :class:`ResumeParseError`.
    """
    body = {
        "model": model,
        "max_tokens": _MAX_TOKENS,
        "system": _SYSTEM_PROMPT,
        "tools": _build_tools(),
        "tool_choice": {"type": "tool", "name": "save_resume"},
        "messages": [{"role": "user", "content": content_blocks}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": _ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        try:
            resp = await client.post(_ANTHROPIC_API_URL, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ResumeParseError(f"http_error:{exc!r}") from exc

    if resp.status_code >= 400:
        # Лог тела ошибки — чтобы можно было диагностировать
        # rate-limit / invalid_request / overloaded.
        snippet = resp.text[:500].replace("\n", " ")
        logger.warning(
            "anthropic API %s: %s", resp.status_code, snippet,
        )
        raise ResumeParseError(
            f"anthropic_http_{resp.status_code}"
        )

    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise ResumeParseError("anthropic_invalid_json") from exc


def _extract_tool_input(response: dict[str, Any]) -> dict[str, Any]:
    """Достать ``input`` из tool_use-блока ответа.

    Структура ответа Anthropic::

        {
            "content": [
                {"type": "tool_use", "name": "save_resume",
                 "input": {...}},
                ...
            ],
            ...
        }
    """
    content = response.get("content") or []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_use" and block.get("name") == "save_resume":
            data = block.get("input")
            if isinstance(data, dict):
                return data
    raise ResumeParseError("anthropic_no_tool_use_in_response")


# ── Публичные точки входа ──────────────────────────────────────────


async def parse_resume_document(
    content_bytes: bytes,
    *,
    mime_type: str | None = None,
    filename: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> ResumeSchema:
    """Распарсить документ-резюме и вернуть валидную pydantic-модель.

    Параметры:
        content_bytes: сырые байты файла.
        mime_type: MIME-тип файла (обычно из ``UploadFile.content_type``).
            Если пуст — пробуем угадать по ``filename``.
        filename: имя исходного файла (нужно, только если ``mime_type``
            не задан) — например, ``"resume.pdf"``.
        api_key: ключ Anthropic API. Если не передан, берётся из
            переменной окружения ``ANTHROPIC_API_KEY``.
        model: имя модели Anthropic. Если не передано, берётся из
            переменной окружения ``ANTHROPIC_MODEL`` (а если и она
            пустая — :data:`_DEFAULT_MODEL`).

    Возвращает:
        :class:`app.schemas.resume.Resume` — структурированное резюме.

    Кидает:
        :class:`ResumeParseError` — на любую ошибку (нет ключа, плохой
        MIME, HTTP-ошибка Anthropic, невалидный ответ модели).
    """
    if not ANTHROPIC_API_KEY:
        raise ResumeParseError("anthropic_api_key_missing")
    use_model = (model or _DEFAULT_MODEL).strip() or _DEFAULT_MODEL

    content_blocks = _build_user_content(
        content_bytes,
        mime_type=mime_type or "",
        filename=filename,
    )

    response = await _call_anthropic(
        content_blocks=content_blocks,
        api_key=ANTHROPIC_API_KEY,
        model=use_model,
    )
    raw_data = _extract_tool_input(response)

    try:
        return ResumeSchema.model_validate(raw_data)
    except ValidationError as exc:
        # Не теряем сырые данные — кладём их в repr, чтобы было что
        # дебажить в логах.
        logger.warning(
            "resume parser: validation failed; raw=%s err=%s",
            json.dumps(raw_data, ensure_ascii=False)[:500],
            exc,
        )
        raise ResumeParseError("anthropic_invalid_resume_schema") from exc


async def parse_resume_file(
    path: Path | str,
    *,
    mime_type: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> ResumeSchema:
    """То же, что :func:`parse_resume_document`, но принимает путь
    к файлу на диске.

    Если ``mime_type`` не указан — угадываем по расширению.
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise ResumeParseError(f"file_not_found:{file_path}")
    # Чтение файла блокирует event loop; для безопасности
    # выносим в default thread-pool.
    blob = await asyncio.to_thread(file_path.read_bytes)
    return await parse_resume_document(
        blob,
        mime_type=mime_type,
        filename=file_path.name,
        api_key=api_key,
        model=model,
    )
