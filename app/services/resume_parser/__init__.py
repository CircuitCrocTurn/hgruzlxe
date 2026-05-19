"""Сервис парсинга загруженных юзером резюме (PDF / image / DOCX / TXT)
через Anthropic Claude API.

В отличие от ``app.services.job_sites.hh.resume_parser`` (который читает
HTML-экспорт hh.ru через BeautifulSoup и работает только по hh-флоу),
этот сервис принимает произвольный файл от юзера и через LLM приводит
его к единой схеме :class:`app.schemas.resume.Resume`.

Точка входа — :func:`parser_resume_documents.parse_resume_document`.
"""
from __future__ import annotations

from app.services.resume_parser.parser_resume_documents import (
    ResumeParseError,
    parse_resume_document,
    parse_resume_file,
)

__all__ = [
    "ResumeParseError",
    "parse_resume_document",
    "parse_resume_file",
]
