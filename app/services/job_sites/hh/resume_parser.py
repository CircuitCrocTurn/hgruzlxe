"""Парсер HTML-резюме с hh.ru (PDF/HTML-экспорт).

HH.ru даёт скачиваемое резюме в виде HTML с CSS-классами вида
``resume__title``, ``resume__block``, ``resume-experience``,
``resume-education``, ``resume-skils``, ``resume-additional`` и т.д.
Этот модуль умеет извлекать оттуда структурированные данные и
возвращать :class:`resume_schema.Resume`.

Использование::

    from resume_parser import resume_parse
    with open("resume.html", encoding="utf-8") as fp:
        resume = resume_parse(fp.read())
    print(resume.model_dump_json(indent=2, exclude_none=True))

CLI::

    python3 resume_parser.py resume.html
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Iterable, List, Optional, Tuple

from bs4 import BeautifulSoup, NavigableString, Tag

from app.schemas.resume import (
    Resume,
    ResumeContact,
    ResumeCourse,
    ResumeDriving,
    ResumeEducation,
    ResumeExperience,
    ResumeLanguage,
    ResumeRecommendation,
)


# ---------------------------------------------------------------------------
# Утилиты
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+", flags=re.UNICODE)
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+")
_PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{7,}\d")


def _clean(text: Optional[str]) -> str:
    """Сжимает пробелы и убирает неразрывные пробелы / лишние переводы строк."""
    if not text:
        return ""
    text = text.replace("\xa0", " ")
    return _WS_RE.sub(" ", text).strip()


def _text_with_breaks(el: Tag) -> str:
    """``el.get_text()``, но ``<br>`` превращается в перевод строки.

    BeautifulSoup-овский ``get_text`` склеивает всё подряд, поэтому в
    больших описаниях опыта работы мы теряем форматирование. Здесь мы
    проходим по детям и сохраняем переводы строк, чтобы было видно
    структуру при последующем чтении.
    """
    chunks: List[str] = []
    for child in el.descendants:
        if isinstance(child, NavigableString):
            chunks.append(str(child))
        elif isinstance(child, Tag) and child.name == "br":
            chunks.append("\n")
    raw = "".join(chunks).replace("\xa0", " ")
    # Сжать множественные переводы строк до максимум двух, обрезать пробелы по строкам.
    lines = [line.strip() for line in raw.splitlines()]
    out: List[str] = []
    blank = 0
    for line in lines:
        if not line:
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(line)
    return "\n".join(out).strip()


def _strip_prefix(text: str, prefix: str) -> str:
    if text.startswith(prefix):
        return text[len(prefix):].strip()
    return text


# ---------------------------------------------------------------------------
# Разбиение документа на секции
# ---------------------------------------------------------------------------


def _find_body(soup: BeautifulSoup) -> Optional[Tag]:
    """``<div class="resume__body">`` — основной контейнер с данными."""
    return soup.find("div", class_="resume__body")


def _split_sections(body: Tag):
    """Делит дочерние элементы ``resume__body`` на «шапку» и секции.

    Возвращает:
      * ``head`` — список узлов до первого ``<p class="resume__block">``.
      * ``sections`` — список ``(имя_секции, [узлы])``.

    Узлами могут быть как ``Tag``, так и ``NavigableString``: hh.ru
    иногда оставляет «голый» текст внутри ``resume__body`` (например,
    «Права категории B, C» в разделе «Опыт вождения»).
    """
    head: List = []
    sections: List[Tuple[str, List]] = []
    current_name: Optional[str] = None
    current_nodes: List = []

    for child in body.children:
        if isinstance(child, NavigableString):
            if not str(child).strip():
                continue
            if current_name is None:
                head.append(child)
            else:
                current_nodes.append(child)
            continue
        if not isinstance(child, Tag):
            continue
        classes = child.get("class") or []
        if child.name == "p" and "resume__block" in classes:
            if current_name is not None:
                sections.append((current_name, current_nodes))
            current_name = _clean(child.get_text())
            current_nodes = []
        else:
            if current_name is None:
                head.append(child)
            else:
                current_nodes.append(child)
    if current_name is not None:
        sections.append((current_name, current_nodes))
    return head, sections


# ---------------------------------------------------------------------------
# Парсинг шапки (имя, пол/возраст, контакты, локация, гражданство, …)
# ---------------------------------------------------------------------------

_GENDER_RE = re.compile(r"^(Мужчина|Женщина)", flags=re.IGNORECASE)
_AGE_RE = re.compile(r"(\d+)\s*(?:лет|год[а]?)", flags=re.IGNORECASE)
_BIRTHDAY_RE = re.compile(r"родил[аcс]+[яь]?\s+([\d]{1,2}\s+[а-яА-ЯёЁ]+\s+\d{4})")
_LABEL_VALUE_RE = re.compile(r"^([a-zA-Zа-яА-ЯёЁ0-9 _\-]+):\s*(.+)$")


def _classify_contact(value: str) -> str:
    if _EMAIL_RE.fullmatch(value):
        return "email"
    if value.startswith("@") or "t.me/" in value or "telegram" in value.lower():
        return "messenger"
    if _PHONE_RE.fullmatch(value):
        return "phone"
    if value.startswith("http://") or value.startswith("https://"):
        return "link"
    return "other"


def _parse_personal_line(text: str, resume: Resume) -> bool:
    """«Мужчина, 25 лет, родился 12 ноября 2000» — пытается распарсить.

    Возвращает True, если строка похожа на «персональную» и была применена.
    """
    g = _GENDER_RE.match(text)
    if not g:
        return False
    resume.gender = g.group(1).capitalize()
    age = _AGE_RE.search(text)
    if age:
        try:
            resume.age = int(age.group(1))
        except ValueError:
            pass
    bd = _BIRTHDAY_RE.search(text)
    if bd:
        resume.birthday = bd.group(1)
    return True


def _parse_head(head: List, resume: Resume) -> None:
    for node in head:
        if not isinstance(node, Tag):
            continue
        # Имя
        if "resume__title" in (node.get("class") or []):
            resume.full_name = _clean(node.get_text(separator=" "))
            continue

        if node.name != "p":
            continue

        text = _clean(node.get_text(separator=" "))
        if not text:
            continue

        # Персональная строка
        if _parse_personal_line(text, resume):
            continue

        # «Проживает: …»
        if text.startswith("Проживает"):
            resume.location = _strip_prefix(text, "Проживает:").strip()
            continue

        # «Гражданство: …, есть разрешение на работу: …»
        if text.startswith("Гражданство"):
            # формат может быть: "Гражданство: Россия, есть разрешение на работу: Россия"
            rest = _strip_prefix(text, "Гражданство:").strip()
            cit_part, _, perm_part = rest.partition("есть разрешение на работу")
            cit_part = cit_part.rstrip(", ").strip()
            if cit_part:
                resume.citizenship = [c.strip() for c in cit_part.split(",") if c.strip()]
            if perm_part:
                perm_part = perm_part.lstrip(":").strip()
                resume.work_permit = [c.strip() for c in perm_part.split(",") if c.strip()]
            continue

        # «Не готов к переезду, не готов к командировкам»
        if "переезд" in text.lower() or "командировк" in text.lower():
            # Разделим на две части по запятой
            parts = [p.strip() for p in re.split(r",", text)]
            for p in parts:
                low = p.lower()
                if "переезд" in low:
                    resume.relocation = p
                elif "командировк" in low:
                    resume.business_trips = p
            continue

        # Контакт: телефон / email / мессенджер / ссылка
        contact = _parse_contact_paragraph(node)
        if contact:
            resume.contacts.append(contact)


def _parse_contact_paragraph(p: Tag) -> Optional[ResumeContact]:
    """Один ``<p>`` в шапке = один контакт + (опц.) подписи в ``<span class="info">``.

    Поддерживает форматы:
      * ``+7 (936) 6084832 <span>— предпочитаемый способ связи</span>``
      * ``+7 (923) 4325434 <span>— это домашний телефон</span>``
      * ``google@gmail.com``
      * ``max: https://max.ru/u/123`` / ``telegram: @aquakey43``
      * ``мой телеграм для связи: t.me/aquakey``
    """
    info_spans = p.find_all("span", class_="info")
    # Достаём текст без info-спанов, чтобы получить «значение» контакта
    clone_texts: List[str] = []
    for child in p.children:
        if isinstance(child, Tag) and "info" in (child.get("class") or []):
            continue
        if isinstance(child, NavigableString):
            clone_texts.append(str(child))
        elif isinstance(child, Tag):
            clone_texts.append(child.get_text(separator=" "))
    main = _clean("".join(clone_texts))
    if not main:
        return None

    label: Optional[str] = None
    comment: Optional[str] = None
    preferred = False
    for span in info_spans:
        info_text = _clean(span.get_text(separator=" "))
        # формат «— подпись»: считаем это label/комментарием
        if info_text.startswith("—"):
            info_text = info_text.lstrip("— ").strip()
        elif info_text.startswith("•"):
            info_text = info_text.lstrip("• ").strip()
        if "предпочитаемый способ связи" in info_text.lower():
            preferred = True
            label = info_text
        elif label is None:
            label = info_text
        else:
            comment = info_text

    # «label: value» — например «telegram: @aquakey43»
    m = _LABEL_VALUE_RE.match(main)
    if m:
        possible_label, value = m.group(1).strip(), m.group(2).strip()
        # Если перед двоеточием стоит явно мессенджер/сайт — пишем как label
        kind = _classify_contact(value)
        if kind == "other":
            # эвристика: telegram/max/whatsapp/skype/icq/…
            low = possible_label.lower()
            if any(k in low for k in ("telegram", "телеграм", "max", "whatsapp",
                                       "viber", "skype", "icq", "linkedin",
                                       "github", "vk", "instagram", "discord")):
                kind = "messenger" if "max" in low or any(
                    k in low for k in ("telegram", "телеграм", "whatsapp",
                                       "viber", "skype", "icq", "discord")
                ) else "link"
        return ResumeContact(
            kind=kind,
            value=value,
            label=label or possible_label,
            comment=comment,
            preferred=preferred,
        )

    kind = _classify_contact(main)
    return ResumeContact(
        kind=kind,
        value=main,
        label=label,
        comment=comment,
        preferred=preferred,
    )


# ---------------------------------------------------------------------------
# Секция «Желаемая должность и зарплата»
# ---------------------------------------------------------------------------


def _parse_desired_position(nodes: List[Tag], resume: Resume) -> None:
    # title
    for n in nodes:
        if "resume__position" in (n.get("class") or []):
            resume.title = _clean(n.get_text(separator=" "))
            break

    # зарплата + «на руки»/«до вычета»
    for n in nodes:
        sal = n.find("span", class_="resume__salary") if isinstance(n, Tag) else None
        if sal is not None:
            resume.salary = _clean(sal.get_text(separator=" "))
            full = _clean(n.get_text(separator=" "))
            # пример: "100 000 ₽ на руки"
            rest = full.replace(resume.salary, "", 1).strip()
            # Currency = первый «символ-токен»
            tokens = rest.split(" ", 1)
            if tokens and tokens[0]:
                resume.currency = tokens[0]
            if len(tokens) > 1:
                resume.salary_kind = tokens[1].strip()
            break

    # Специализации
    for n in nodes:
        if isinstance(n, Tag) and (
            n.name == "ul" and "resume-profession-roles" in (n.get("class") or [])
        ):
            for li in n.find_all("li"):
                txt = _clean(li.get_text(separator=" "))
                if txt:
                    resume.specializations.append(txt)

    # Текстовые параграфы: «Тип занятости», «Формат работы», «Желательное время…»
    for n in nodes:
        if n.name != "p":
            continue
        text = _clean(n.get_text(separator=" "))
        if text.startswith("Тип занятости"):
            resume.employment_type = _strip_prefix(text, "Тип занятости:").strip()
        elif text.startswith("Формат работы"):
            resume.work_format = _strip_prefix(text, "Формат работы:").strip()
        elif text.startswith("Желательное время в пути"):
            resume.commute_time = _strip_prefix(
                text, "Желательное время в пути до работы:"
            ).strip()


# ---------------------------------------------------------------------------
# Секция «Опыт работы»
# ---------------------------------------------------------------------------

# Шаблон «Декабрь 2022 — настоящее время / 3 года 6 месяцев»
_PERIOD_RE = re.compile(
    r"^(?P<start>[А-Яа-яёЁ]+\s+\d{4})\s*[—\-–]\s*"
    r"(?P<end>настоящее время|[А-Яа-яёЁ]+\s+\d{4})"
    r"(?:\s*(?P<dur>.+))?$"
)


def _parse_experience_section(
    nodes: List[Tag], resume: Resume, section_name: str
) -> None:
    # Длительность общего стажа из имени секции: «Опыт работы — 3 года 6 месяцев»
    if "—" in section_name:
        resume.total_experience = section_name.split("—", 1)[1].strip()

    for n in nodes:
        if not isinstance(n, Tag):
            continue
        for li in n.find_all("li", class_="resume-experience"):
            resume.experience.append(_parse_experience_item(li))


def _parse_experience_item(li: Tag) -> ResumeExperience:
    company_el = li.find("span", class_="resume-experience__company")
    info_el = li.find("p", class_="info")
    hint_el = li.find("p", class_="bloko-form-hint")
    pos_el = li.find("p", class_="resume-experience__position")

    start = end = duration = None
    if hint_el is not None:
        period_text = _clean(hint_el.get_text(separator=" "))
        m = _PERIOD_RE.match(period_text)
        if m:
            start = m.group("start").strip()
            end = m.group("end").strip()
            duration = (m.group("dur") or "").strip() or None
        else:
            duration = period_text or None

    # Описание — следующий <p> после позиции (без специального класса).
    description = None
    if pos_el is not None:
        nxt = pos_el.find_next_sibling()
        while nxt is not None and nxt.name != "p":
            nxt = nxt.find_next_sibling()
        if nxt is not None and nxt is not hint_el and nxt is not info_el:
            description = _text_with_breaks(nxt)
    if description is None:
        # фолбэк: последний <p> без известного класса
        candidates = [
            c for c in li.find_all("p")
            if c is not info_el and c is not hint_el and c is not pos_el
        ]
        if candidates:
            description = _text_with_breaks(candidates[-1])

    return ResumeExperience(
        company=_clean(company_el.get_text(separator=" ")) if company_el else None,
        location=_clean(info_el.get_text(separator=" ")) if info_el else None,
        position=_clean(pos_el.get_text(separator=" ")) if pos_el else None,
        start=start,
        end=end,
        duration=duration,
        description=description or None,
    )


# ---------------------------------------------------------------------------
# Образование / Курсы / Тесты / Сертификаты
# ---------------------------------------------------------------------------


def _education_items(nodes: List[Tag]) -> Iterable[Tag]:
    """Все ``<li class="resume-education">`` в секции, в порядке документа."""
    for n in nodes:
        if not isinstance(n, Tag):
            continue
        for li in n.find_all("li", class_="resume-education"):
            yield li


def _trailing_p_after(li: Tag) -> Optional[str]:
    """``<p>`` сразу после ``<li class="resume-education">`` — факультет/орг-я.

    В hh-экспорте это «висячий» абзац сразу за ``</li>``; он лежит на том же
    уровне внутри ``<ul>``. Берём первый ``<p>`` без специального класса.
    """
    sib = li.find_next_sibling()
    while sib is not None:
        if isinstance(sib, Tag):
            if sib.name == "p":
                cls = sib.get("class") or []
                if "bloko-form-hint" in cls or "info" in cls:
                    sib = sib.find_next_sibling()
                    continue
                return _clean(sib.get_text(separator=" "))
            if sib.name == "li":  # пошёл следующий пункт
                return None
        sib = sib.find_next_sibling()
    return None


def _parse_education_section(nodes: List[Tag], resume: Resume) -> None:
    level_hint: Optional[str] = None
    # Первый <li> без класса с уровнем образования (e.g. "Неоконченное высшее")
    for n in nodes:
        if not isinstance(n, Tag):
            continue
        for li in n.find_all("li"):
            if "resume-education" in (li.get("class") or []):
                break
            txt = _clean(li.get_text(separator=" "))
            if txt:
                level_hint = txt
                break
        if level_hint:
            break

    for li in _education_items(nodes):
        name_el = li.find("span", class_="resume-education__name")
        hints = li.find_all("p", class_="bloko-form-hint")
        year = _clean(hints[0].get_text(separator=" ")) if hints else None
        level = _clean(hints[1].get_text(separator=" ")) if len(hints) >= 2 else level_hint

        faculty = _trailing_p_after(li)

        resume.education.append(
            ResumeEducation(
                institution=_clean(name_el.get_text(separator=" ")) if name_el else None,
                faculty=faculty,
                year=year,
                level=level,
            )
        )


def _parse_courselike_section(
    nodes: List[Tag],
) -> List[ResumeCourse]:
    """Парсит «Повышение квалификации, курсы», «Тесты, экзамены»,
    «Электронные сертификаты» — у всех структура одинаковая."""
    out: List[ResumeCourse] = []
    for li in _education_items(nodes):
        name_el = li.find("span", class_="resume-education__name")
        hints = li.find_all("p", class_="bloko-form-hint")
        year = _clean(hints[0].get_text(separator=" ")) if hints else None
        tail = _trailing_p_after(li)
        organization = None
        specialization = None
        if tail:
            # Часто формат: «Stepik, middle python разработчик»
            if "," in tail:
                organization, _, specialization = tail.partition(",")
                organization = organization.strip() or None
                specialization = specialization.strip() or None
            else:
                organization = tail
        out.append(
            ResumeCourse(
                name=_clean(name_el.get_text(separator=" ")) if name_el else None,
                year=year,
                organization=organization,
                specialization=specialization,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Секция «Навыки» — языки + скиллы
# ---------------------------------------------------------------------------


def _parse_skills_section(nodes: List[Tag], resume: Resume) -> None:
    for n in nodes:
        if not isinstance(n, Tag):
            continue
        for skill_li in n.find_all("li", class_="resume-skils"):
            _parse_skills_block(skill_li, resume)


def _parse_skills_block(li: Tag, resume: Resume) -> None:
    label_el = li.find("span", class_="bloko-form-hint")
    label = _clean(label_el.get_text(separator=" ")) if label_el else ""

    if "язык" in label.lower():
        # <ul class="resume-skils__item"><li>Русский <span class="info"> — Родной</span></li></ul>
        ul = li.find("ul", class_="resume-skils__item")
        if ul is None:
            return
        for lang_li in ul.find_all("li", recursive=False):
            info = lang_li.find("span", class_="info")
            level: Optional[str] = None
            if info is not None:
                level_text = _clean(info.get_text(separator=" "))
                level = level_text.lstrip("— ").strip() or None
                info.extract()
            name = _clean(lang_li.get_text(separator=" "))
            if name:
                resume.languages.append(ResumeLanguage(name=name, level=level))
    elif "навык" in label.lower():
        # <p class="resume-skils__item"><span>Git; </span><span>Python; </span>…</p>
        p = li.find("p", class_="resume-skils__item")
        if p is None:
            return
        for span in p.find_all("span"):
            raw = _clean(span.get_text(separator=" "))
            raw = raw.rstrip(";").strip()
            if raw:
                resume.skills.append(raw)


# ---------------------------------------------------------------------------
# Секция «Опыт вождения»
# ---------------------------------------------------------------------------

_DRIVING_CAT_RE = re.compile(r"категории\s+([A-ZА-Я0-9,\s]+)", flags=re.IGNORECASE)


def _parse_driving_section(nodes: List[Tag], resume: Resume) -> None:
    driving = ResumeDriving()
    text_parts: List[str] = []
    for n in nodes:
        if isinstance(n, Tag):
            text_parts.append(_clean(n.get_text(separator=" ")))
        elif isinstance(n, NavigableString):
            text_parts.append(_clean(str(n)))
    text = " ".join(t for t in text_parts if t)
    if "собственный автомобиль" in text.lower():
        driving.has_own_car = True
    m = _DRIVING_CAT_RE.search(text)
    if m:
        cats = [c.strip() for c in re.split(r"[,\s]+", m.group(1)) if c.strip()]
        driving.categories = cats
    # Сохраняем только если что-то реально нашли.
    if driving.has_own_car or driving.categories:
        resume.driving = driving


# ---------------------------------------------------------------------------
# Секция «Дополнительная информация» — рекомендации + «Обо мне»
# ---------------------------------------------------------------------------


def _parse_additional_section(nodes: List[Tag], resume: Resume) -> None:
    for n in nodes:
        if not isinstance(n, Tag):
            continue

        # Рекомендации
        for add_li in n.find_all("li", class_="resume-additional"):
            label_el = add_li.find("span", class_="bloko-form-hint")
            label = _clean(label_el.get_text(separator=" ")) if label_el else ""
            if "рекомендац" in label.lower():
                for rec_li in add_li.find_all("li", class_="resume-recomendation"):
                    info = rec_li.find("p", class_="info")
                    person = (
                        _clean(info.get_text(separator=" ")) if info is not None else None
                    )
                    if info is not None:
                        info.extract()
                    org = _clean(rec_li.get_text(separator=" "))
                    resume.recommendations.append(
                        ResumeRecommendation(organization=org or None, person=person)
                    )

        # «Обо мне»
        for skills_li in n.find_all("li", class_="resume-skils"):
            label_el = skills_li.find("span", class_="bloko-form-hint")
            label = _clean(label_el.get_text(separator=" ")) if label_el else ""
            if "обо мне" in label.lower():
                p = skills_li.find("p", class_="resume-skils__item")
                if p is not None:
                    resume.summary = _text_with_breaks(p) or None


# ---------------------------------------------------------------------------
# Метаданные шапки (дата обновления)
# ---------------------------------------------------------------------------

_UPDATED_RE = re.compile(r"Резюме обновлено\s+(.+)$", flags=re.IGNORECASE)


def _parse_metadata(soup: BeautifulSoup, resume: Resume) -> None:
    footer = soup.find("p", class_="info-footer")
    if footer is None:
        return
    m = _UPDATED_RE.search(_clean(footer.get_text(separator=" ")))
    if m:
        resume.updated_at = m.group(1).strip()


# ---------------------------------------------------------------------------
# Диспетчер секций
# ---------------------------------------------------------------------------


def _dispatch_section(name: str, nodes: List[Tag], resume: Resume) -> None:
    """Привязывает имя секции к функции-обработчику."""
    low = name.lower()
    if "желаемая должность" in low:
        _parse_desired_position(nodes, resume)
    elif low.startswith("опыт работы"):
        _parse_experience_section(nodes, resume, name)
    elif low.startswith("образование"):
        _parse_education_section(nodes, resume)
    elif "повышение квалификации" in low or low.startswith("курсы"):
        resume.courses.extend(_parse_courselike_section(nodes))
    elif "тесты" in low or "экзамен" in low:
        resume.tests.extend(_parse_courselike_section(nodes))
    elif "сертификат" in low:
        resume.certificates.extend(_parse_courselike_section(nodes))
    elif low.startswith("навык"):
        _parse_skills_section(nodes, resume)
    elif "вожден" in low:
        _parse_driving_section(nodes, resume)
    elif "дополнительная информация" in low:
        _parse_additional_section(nodes, resume)
    # Неизвестная секция — молча игнорируем; raw_text всё равно остаётся.


# ---------------------------------------------------------------------------
# Публичная функция
# ---------------------------------------------------------------------------


def resume_parse(html: str) -> Resume:
    """Разбирает HTML-резюме hh.ru и возвращает :class:`Resume`."""
    soup = BeautifulSoup(html, "html.parser")
    resume = Resume()

    _parse_metadata(soup, resume)

    body = _find_body(soup)
    if body is None:
        # совсем не похоже на hh.ru — сохраним хотя бы raw_text
        resume.raw_text = _clean(soup.get_text(separator="\n"))
        return resume

    head, sections = _split_sections(body)
    _parse_head(head, resume)
    for name, nodes in sections:
        _dispatch_section(name, nodes, resume)

    # Удобные «плоские» поля для совместимости.
    for c in resume.contacts:
        if c.kind == "email" and resume.email is None:
            resume.email = c.value
        if c.kind == "phone" and (resume.phone is None or c.preferred):
            resume.phone = c.value

    # Полный текст резюме — пригодится для AI-промптов.
    resume.raw_text = _text_with_breaks(body)

    return resume


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Парсер HTML-резюме hh.ru")
    p.add_argument("html_path", help="Путь к HTML-резюме")
    p.add_argument(
        "--no-raw-text",
        action="store_true",
        help="Не включать raw_text в JSON (для компактного вывода)",
    )
    args = p.parse_args(argv)

    with open(args.html_path, "r", encoding="utf-8") as fp:
        html = fp.read()

    resume = resume_parse(html)
    dump = resume.model_dump(exclude_none=True)
    if args.no_raw_text:
        dump.pop("raw_text", None)

    import json
    sys.stdout.write(json.dumps(dump, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
