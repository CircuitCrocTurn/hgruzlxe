"""
habr_vacancy_extractor.py
=========================

Извлекает текст вакансии (от названия до ключевых навыков) из HTML-страницы
career.habr.com (Хабр Карьера).

Опирается на устойчивые CSS-классы Хабр Карьеры (``page-title__title``,
``basic-salary``, ``vacancy-description__text``, ``basic-chip…`` и т.д.) и
на блок ``<script type="application/ld+json">`` со схемой ``JobPosting``,
который сайт всегда вставляет в ``<head>``. Это делает извлечение
устойчивым к правкам верстки: даже если поменяются обёртки и порядок
блоков, JSON-LD и базовые BEM-классы карточки переживают редизайн.

Использование:
    python habr_vacancy_extractor.py path/to/vacancy.html
    python habr_vacancy_extractor.py file1.html file2.html  -o out_dir
    python habr_vacancy_extractor.py file.html --json
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from bs4 import BeautifulSoup, Tag


from app.schemas.vacancy import Vacancy

# ---------------------------------------------------------------------------
# Карта полей: какие CSS-селекторы соответствуют каким полям вакансии.
# Для полей, которые надёжнее доставать из JSON-LD (название компании,
# тип занятости), селектор оставлен пустым и используется как fallback.
# ---------------------------------------------------------------------------
FIELD_SELECTORS = {
    "title":       "h1.page-title__title",                       # Название вакансии
    "salary":      ".vacancy-header__salary .basic-salary",      # Зарплата (может отсутствовать)
    "experience":  "",                                           # Грейд (Middle/Senior/…) — см. _get_experience
    "employment":  "",                                           # Тип занятости / формат — см. _get_employment
    "company":     ".company_name a, .vacancy-company__name a",  # Название компании (fallback к JSON-LD)
    "description": ".vacancy-description__text",                 # Описание вакансии (большой блок)
}
SKILLS_SELECTOR = ".basic-chip--color-ui-gray-4 .chip-without-icon__text"  # Каждый бейдж навыка


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------
def _clean(text: str) -> str:
    """Схлопывает множественные пробелы/переводы строк, но сохраняет абзацы."""
    if not text:
        return ""
    # \xa0 — неразрывный пробел (Хабр любит его вставлять в числах зарплаты)
    text = text.replace("\xa0", " ")
    # Подряд идущие пустые строки -> один пустой разделитель
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Лишний пробел перед знаками препинания (артефакт склейки <span>'ов)
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return text.strip()


def _get_text(soup: BeautifulSoup, selector: str, separator: str = " ") -> str:
    """Достаёт текст элемента по CSS-селектору (или '' если нет).

    separator=" "  — для коротких однострочных полей (title, salary, …);
    separator="\\n" — для description, чтобы сохранить разбиение на абзацы.
    """
    if not selector:
        return ""
    node: Optional[Tag] = soup.select_one(selector)
    if node is None:
        return ""
    return _clean(node.get_text(separator, strip=True))


def _get_skills(soup: BeautifulSoup) -> List[str]:
    """Возвращает список ключевых навыков без дубликатов, в исходном порядке."""
    seen, out = set(), []
    for node in soup.select(SKILLS_SELECTOR):
        skill = _clean(node.get_text(" ", strip=True))
        if skill and skill not in seen:
            seen.add(skill)
            out.append(skill)
    return out


# Возможные «места», где Хабр Карьера держит логотип. Проверяем по очереди,
# первый найденный URL (не дефолтную заглушку) и возвращаем.
_LOGO_SELECTOR_CANDIDATES: tuple[dict, ...] = (
    {"name": "img", "attrs": {"class": "company_logo"}},
    {"name": "img", "attrs": {"class": re.compile(r"company[-_]logo", re.I)}},
    {"name": "img", "attrs": {"alt": re.compile(r"logo|логотип", re.I)}},
    {"name": "meta", "attrs": {"property": "og:image"}},
    {"name": "meta", "attrs": {"name": "twitter:image"}},
)


def _get_company_logo_url(soup: BeautifulSoup) -> str:
    """Пытается достать абсолютный URL логотипа компании со страницы вакансии.

    Возвращает пустую строку, если не нашли — воркер тогда просто не
    скачает логотип и карточка покажет инициалы.
    """
    # Первый проход — пропускаем дефолтные плейсхолдеры
    # (``…/defaults/companies/logo/medium_default-…png``).
    fallback = ""
    for cand in _LOGO_SELECTOR_CANDIDATES:
        for node in soup.find_all(cand["name"], attrs=cand["attrs"]):
            url = (node.get("content") or node.get("src") or "").strip()
            if not url:
                continue
            if "default" in url:
                fallback = fallback or url
                continue
            return url
    return fallback


# ---------------------------------------------------------------------------
# JSON-LD: Хабр всегда кладёт в <head> блок schema.org/JobPosting.
# Это самый надёжный источник для названия компании, типа занятости и URL.
# ---------------------------------------------------------------------------
def _get_jsonld_jobposting(soup: BeautifulSoup) -> dict:
    """Возвращает dict с полями schema.org/JobPosting или {} если нет."""
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("@type") == "JobPosting":
                    return item
    return {}


_EMPLOYMENT_TYPE_RU = {
    "FULL_TIME":  "Полная занятость",
    "PART_TIME":  "Частичная занятость",
    "CONTRACTOR": "Подрядная работа",
    "TEMPORARY":  "Временная занятость",
    "INTERN":     "Стажировка",
    "VOLUNTEER":  "Волонтёрство",
    "PER_DIEM":   "Подённая работа",
    "OTHER":      "Другое",
}


def _get_experience(soup: BeautifulSoup) -> str:
    """Грейд кандидата (Junior / Middle / Senior / …).

    На Хабр Карьере он отображается бирюзовым чипом с иконкой grade
    в блоке «Требования».
    """
    for chip in soup.select(".basic-chip--color-ui-turquoise"):
        if chip.select_one(".svg-icon--icon-grade"):
            text_node = chip.select_one(".chip-with-icon__text")
            if text_node:
                return _clean(text_node.get_text(" ", strip=True))
    return ""


def _get_employment(soup: BeautifulSoup, jsonld: dict) -> str:
    """Тип занятости + формат работы (склеенные через запятую).

    Берём ``employmentType`` из JSON-LD (FULL_TIME → «Полная занятость»)
    и формат работы из серого чипа («Можно удалённо» / «В офисе» / …).
    """
    parts: List[str] = []

    et = jsonld.get("employmentType", "")
    if isinstance(et, list):
        for code in et:
            ru = _EMPLOYMENT_TYPE_RU.get(str(code).upper())
            if ru and ru not in parts:
                parts.append(ru)
    elif isinstance(et, str) and et:
        ru = _EMPLOYMENT_TYPE_RU.get(et.upper())
        if ru:
            parts.append(ru)

    for chip in soup.select(".basic-chip--color-ui-gray-3"):
        if chip.select_one(".svg-icon--icon-format"):
            text_node = chip.select_one(".chip-with-icon__text")
            if text_node:
                fmt = _clean(text_node.get_text(" ", strip=True))
                if fmt and fmt not in parts:
                    parts.append(fmt)
            break

    return ", ".join(parts)


def _get_company(soup: BeautifulSoup, jsonld: dict) -> str:
    """Название компании. JSON-LD надёжнее, чем разметка карточки."""
    name = ""
    org = jsonld.get("hiringOrganization") if jsonld else None
    if isinstance(org, dict):
        name = org.get("name", "") or ""
    if name:
        return _clean(name)
    return _get_text(soup, FIELD_SELECTORS["company"])


def _get_link(soup: BeautifulSoup, jsonld: dict) -> str:
    """Канонический URL вакансии."""
    node = soup.find("link", rel="canonical")
    if node and node.get("href"):
        return node["href"].strip()
    url = jsonld.get("url") if jsonld else ""
    if isinstance(url, str) and url:
        return url.strip()
    return ""


# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------
def extract_vacancy(html: str) -> Vacancy:
    """
    Принимает HTML страницы вакансии Хабр Карьеры, возвращает структуру Vacancy.

    Работает на любом подобном HTML-файле с career.habr.com: ищет блоки по
    устойчивым CSS-классам и JSON-LD, а не по конкретной обёртке, поэтому
    верстку можно менять — извлечение не сломается.
    """
    soup = BeautifulSoup(html, "html.parser")
    jsonld = _get_jsonld_jobposting(soup)
    return Vacancy(
        title=_get_text(soup, FIELD_SELECTORS["title"]) or _clean(jsonld.get("title", "")),
        company=_get_company(soup, jsonld),
        salary=_get_text(soup, FIELD_SELECTORS["salary"]),
        experience=_get_experience(soup),
        employment=_get_employment(soup, jsonld),
        description=_get_text(soup, FIELD_SELECTORS["description"], separator="\n"),
        skills=_get_skills(soup),
        link=_get_link(soup, jsonld),
        company_logo=_get_company_logo_url(soup),
    )


def extract_from_file(path: str | Path) -> Vacancy:
    path = Path(path)
    html = path.read_text(encoding="utf-8", errors="replace")
    return extract_vacancy(html)
