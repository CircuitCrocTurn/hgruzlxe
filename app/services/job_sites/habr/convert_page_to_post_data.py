#!/usr/bin/env python3
"""
html_to_post.py
===============

Утилита, которая по HTML-странице с формой строит тело HTTP POST-запроса
в формате ``application/x-www-form-urlencoded`` – ровно так, как это сделал
бы браузер при нажатии submit.

Использование
-------------

    python3 html_to_post.py path/to/page.html
    python3 html_to_post.py path/to/page.html --form-action /onboarding/profile
    python3 html_to_post.py path/to/page.html --form-index 2

По умолчанию скрипт ищет первый ``<form>`` с атрибутом ``action``,
содержащим ``/onboarding/`` (как в приложенных HTML). При желании можно
явно указать форму:

* ``--form-action SUBSTR`` – выбирает форму, action которой содержит
  подстроку ``SUBSTR``;
* ``--form-id ID``         – выбирает форму по атрибуту ``id``;
* ``--form-index N``       – выбирает N-ю форму (0-индексация);
* ``--form-class CLASS``   – выбирает форму по CSS-классу.

Модуль также экспортирует функцию :func:`build_post_body`, пригодную
для программного использования::

    from html_to_post import build_post_body
    body = build_post_body(open("page.html").read())
"""

from __future__ import annotations

import argparse
import sys
from typing import Iterable, List, Optional, Tuple
from urllib.parse import quote_plus

from bs4 import BeautifulSoup, Tag


# Типы <input>, которые браузер НЕ отправляет в теле формы
# (кроме submit/image — они отправляются только при клике по ним,
#  а мы имитируем submit без конкретной кнопки).
_SKIP_INPUT_TYPES = {"submit", "button", "reset", "file", "image"}


def _iter_form_controls(form: Tag) -> Iterable[Tag]:
    """Возвращает элементы формы в порядке их появления в документе."""
    return form.find_all(["input", "select", "textarea"])


def _select_value_pairs(select: Tag) -> List[Tuple[str, str]]:
    """Собирает пары (name, value) для одного <select>.

    * Для ``<select multiple>`` включаются все опции с ``selected``.
    * Для обычного ``<select>`` — первая опция с ``selected``.
      Если ни одна не помечена, браузер выбирает первую непустую опцию.
    """
    name = select.get("name")
    if not name:
        return []
    options = select.find_all("option")
    selected = [o for o in options if o.has_attr("selected")]
    multiple = select.has_attr("multiple")

    if multiple:
        # Браузер шлёт пустое значение для пустого multiple-select?
        # Нет: если ничего не выбрано, поле не отправляется вовсе.
        chosen = selected
    else:
        if selected:
            chosen = [selected[0]]
        elif options:
            chosen = [options[0]]
        else:
            chosen = []

    pairs: List[Tuple[str, str]] = []
    for opt in chosen:
        # У <option> значение — это атрибут value, а при его отсутствии —
        # текст опции (по спецификации HTML).
        if opt.has_attr("value"):
            val = opt["value"]
        else:
            val = opt.get_text()
        pairs.append((name, val))
    return pairs


def _extract_pairs(form: Tag) -> List[Tuple[str, str]]:
    """Извлекает все (name, value) из переданной формы в порядке документа."""
    pairs: List[Tuple[str, str]] = []
    for el in _iter_form_controls(form):
        if el.has_attr("disabled"):
            continue
        name = el.get("name")
        if not name:
            continue

        tag = el.name
        if tag == "input":
            itype = (el.get("type") or "text").lower()
            if itype in _SKIP_INPUT_TYPES:
                continue
            if itype in ("checkbox", "radio"):
                if not el.has_attr("checked"):
                    continue
                # Значение checkbox/radio по умолчанию = "on"
                value = el.get("value", "on")
            else:
                value = el.get("value", "")
            pairs.append((name, value))

        elif tag == "select":
            pairs.extend(_select_value_pairs(el))

        elif tag == "textarea":
            # У <textarea> значение — это его содержимое. Начальный
            # перевод строки, если он есть сразу после открывающего тега,
            # браузерами игнорируется (HTML-спека).
            text = el.string if el.string is not None else el.get_text()
            if text is None:
                text = ""
            if text.startswith("\r\n"):
                text = text[2:]
            elif text.startswith("\n"):
                text = text[1:]
            pairs.append((name, text))

    return pairs


def _urlencode(pairs: Iterable[Tuple[str, str]]) -> str:
    """URL-кодирование в формате application/x-www-form-urlencoded.

    Используется кодировка UTF-8; пробел кодируется как ``+``. Это
    поведение по умолчанию у браузеров и у :func:`urllib.parse.quote_plus`.
    """
    return "&".join(f"{quote_plus(n)}={quote_plus(v)}" for n, v in pairs)


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------


def find_form(
    soup: BeautifulSoup,
    *,
    form_action: Optional[str] = None,
    form_id: Optional[str] = None,
    form_class: Optional[str] = None,
    form_index: Optional[int] = None,
) -> Tag:
    """Находит нужную ``<form>`` в HTML.

    Порядок приоритетов селекторов:
    ``form_id`` → ``form_action`` → ``form_class`` → ``form_index`` →
    первая форма с ``action`` содержащим ``/onboarding/`` → первая форма вообще.
    """
    if form_id is not None:
        form = soup.find("form", id=form_id)
        if form is None:
            raise LookupError(f"Форма с id={form_id!r} не найдена")
        return form

    if form_action is not None:
        form = soup.find(
            "form",
            action=lambda a: a is not None and form_action in a,
        )
        if form is None:
            raise LookupError(
                f"Форма с action, содержащим {form_action!r}, не найдена"
            )
        return form

    if form_class is not None:
        form = soup.find("form", class_=form_class)
        if form is None:
            raise LookupError(f"Форма с class={form_class!r} не найдена")
        return form

    forms = soup.find_all("form")
    if form_index is not None:
        if form_index >= len(forms):
            raise LookupError(
                f"В документе {len(forms)} форм, запрошен индекс {form_index}"
            )
        return forms[form_index]

    # Эвристика для приложенных HTML-страниц:
    for f in forms:
        action = f.get("action", "") or ""
        if "/onboarding/" in action:
            return f

    if not forms:
        raise LookupError("В документе нет ни одной <form>")
    return forms[0]


def build_post_body(
    html: str,
    *,
    form_action: Optional[str] = None,
    form_id: Optional[str] = None,
    form_class: Optional[str] = None,
    form_index: Optional[int] = None,
) -> str:
    """Возвращает URL-encoded тело POST-запроса, полученное из формы HTML."""
    soup = BeautifulSoup(html, "html.parser")
    form = find_form(
        soup,
        form_action=form_action,
        form_id=form_id,
        form_class=form_class,
        form_index=form_index,
    )
    return _urlencode(_extract_pairs(form))


def build_post_pairs(
    html: str,
    **kwargs,
) -> List[Tuple[str, str]]:
    """Возвращает (name, value)-пары без URL-кодирования — удобно для requests."""
    soup = BeautifulSoup(html, "html.parser")
    form = find_form(soup, **kwargs)
    return _extract_pairs(form)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Строит тело POST-запроса по HTML-форме.",
    )
    parser.add_argument("html_path", help="Путь к HTML-файлу")
    parser.add_argument(
        "--form-action",
        help="Выбрать форму, action которой содержит эту подстроку",
    )
    parser.add_argument("--form-id", help="Выбрать форму по id")
    parser.add_argument("--form-class", help="Выбрать форму по CSS-классу")
    parser.add_argument(
        "--form-index",
        type=int,
        help="Выбрать N-ю форму (0-индексация)",
    )
    args = parser.parse_args(argv)

    with open(args.html_path, "r", encoding="utf-8") as fp:
        html = fp.read()

    body = build_post_body(
        html,
        form_action=args.form_action,
        form_id=args.form_id,
        form_class=args.form_class,
        form_index=args.form_index,
    )
    # Один перевод строки в конце — удобно при копировании в curl/HTTP-клиент.
    sys.stdout.write(body + "\n")
    return 0
