"""
hh_vacancy_extractor.py
=======================

Извлекает текст вакансии (от названия до ключевых навыков) из HTML-страницы hh.ru.

Опирается на устойчивые атрибуты data-qa, которыми hh.ru размечает значимые
блоки. Эти атрибуты не зависят от смены CSS-классов и редизайнов, поэтому
скрипт работает на любом HTML-файле вакансии с того же сайта.

Использование:
    python hh_vacancy_extractor.py path/to/vacancy.html
    python hh_vacancy_extractor.py file1.html file2.html  -o out_dir
    python hh_vacancy_extractor.py file.html --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

from bs4 import BeautifulSoup, Tag

from app.schemas.vacancy import Vacancy

# ---------------------------------------------------------------------------
# Карта полей: какие data-qa атрибуты соответствуют каким полям вакансии.
# ---------------------------------------------------------------------------
FIELD_SELECTORS = {
    "title":       "vacancy-title",            # Название вакансии
    "salary":      "vacancy-salary",           # Зарплата (может отсутствовать)
    "experience":  "vacancy-experience",       # Требуемый опыт
    "employment":  "common-employment-text",   # Тип занятости / график
    "company":     "vacancy-company-name",     # Название компании
    "description": "vacancy-description",      # Описание вакансии (большой блок)
}
SKILLS_SELECTOR = "skills-element"             # Каждый бейдж навыка


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------
def _clean(text: str) -> str:
    """Схлопывает множественные пробелы/переводы строк, но сохраняет абзацы."""
    if not text:
        return ""
    # \xa0 — неразрывный пробел (hh любит его вставлять в числах зарплаты)
    text = text.replace("\xa0", " ")
    # Подряд идущие пустые строки -> один пустой разделитель
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Лишний пробел перед знаками препинания (артефакт склейки <span>'ов)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip()


def _get_text(soup: BeautifulSoup, data_qa: str, separator: str = " ") -> str:
    """Достаёт текст элемента с заданным data-qa (или '' если нет).

    separator=" "  — для коротких однострочных полей (title, salary, …);
    separator="\n" — для description, чтобы сохранить разбиение на абзацы.
    """
    node: Optional[Tag] = soup.find(attrs={"data-qa": data_qa})
    if node is None:
        return ""
    return _clean(node.get_text(separator, strip=True))


def _get_skills(soup: BeautifulSoup) -> List[str]:
    """Возвращает список ключевых навыков без дубликатов, в исходном порядке."""
    seen, out = set(), []
    for node in soup.find_all(attrs={"data-qa": SKILLS_SELECTOR}):
        skill = _clean(node.get_text(" ", strip=True))
        if skill and skill not in seen:
            seen.add(skill)
            out.append(skill)
    return out


# Возможные «места», где hh.ru держит логотип. Проверяем по очереди,
# первый найденный URL и возвращаем.
_LOGO_SELECTOR_CANDIDATES: tuple[dict, ...] = (
    {"name": "img", "attrs": {"data-qa": "vacancy-company-logo"}},
    {"name": "img", "attrs": {"data-qa": "vacancy-company__logo"}},
    {"name": "img", "attrs": {"class": "bloko-employer-logo__image"}},
    {"name": "img", "attrs": {"class": "company-logo-pic"}},
    {"name": "img", "attrs": {"alt": re.compile(r"logo|логотип", re.I)}},
)


def _get_company_logo_url(soup: BeautifulSoup) -> str:
    """Пытается достать абсолютный URL логотипа компании со страницы вакансии.

    Возвращает пустую строку, если не нашли — воркер тогда просто не
    скачает логотип и карточка покажет инициалы.
    """
    for cand in _LOGO_SELECTOR_CANDIDATES:
        node = soup.find(cand["name"], attrs=cand["attrs"])
        if node and node.get("src"):
            return node["src"].strip()
    # Фолбэк: <picture><source srcset="..."> в карточке компании.
    src_node = soup.select_one(
        "[data-qa='vacancy-company-logo'] img,"
        "[data-qa='vacancy-company__logo'] img"
    )
    if src_node and src_node.get("src"):
        return src_node["src"].strip()
    return ""


# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------
def extract_vacancy(html: str) -> Vacancy:
    """
    Принимает HTML страницы вакансии hh.ru, возвращает структуру Vacancy.

    Работает на любом подобном HTML-файле с hh.ru: ищет блоки по data-qa,
    а не по классам, поэтому верстку можно менять — извлечение не сломается.
    """
    soup = BeautifulSoup(html, "html.parser")
    return Vacancy(
        title=_get_text(soup, FIELD_SELECTORS["title"]),
        company=_get_text(soup, FIELD_SELECTORS["company"]),
        salary=_get_text(soup, FIELD_SELECTORS["salary"]),
        experience=_get_text(soup, FIELD_SELECTORS["experience"]),
        employment=_get_text(soup, FIELD_SELECTORS["employment"]),
        description=_get_text(soup, FIELD_SELECTORS["description"], separator="\n"),
        skills=_get_skills(soup),
        company_logo=_get_company_logo_url(soup),
    )


def extract_from_file(path: str | Path) -> Vacancy:
    path = Path(path)
    html = path.read_text(encoding="utf-8", errors="replace")
    return extract_vacancy(html)