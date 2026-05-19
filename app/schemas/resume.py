"""Pydantic schema for a parsed hh.ru resume.

Расширенная схема под HTML-экспорт hh.ru (PDF-view). Все поля Optional:
если в конкретном резюме секции нет — поле просто остаётся пустым / None.

Stage-2 fraud-detection (см. ``app.queue.jobs.run_identity_check_job``)
сравнивает юзеров по ``email`` + ``full_name``. Поэтому здесь должны быть
поля ``email`` и (на будущее) ``education`` — даже если парсер их пока
не извлекает. Когда юзер добавит парсинг почты в свой ``resume_parser``,
поля автоматически окажутся в БД через ``resume_to_db`` (хранятся в
``Resume.parsed_data`` JSONB).
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Под-объекты
# ---------------------------------------------------------------------------


class ResumeContact(BaseModel):
    """Один контакт — телефон/email/мессенджер/ссылка."""

    kind: str  # "phone" | "email" | "messenger" | "link" | "other"
    value: str
    label: Optional[str] = None  # "предпочитаемый способ связи", "домашний телефон" и т.п.
    comment: Optional[str] = None  # произвольный комментарий рядом
    preferred: bool = False  # помечен как «предпочитаемый способ связи»


class ResumeExperience(BaseModel):
    company: Optional[str] = None
    location: Optional[str] = None
    position: Optional[str] = None
    start: Optional[str] = None  # как написано в hh, например "Декабрь 2022"
    end: Optional[str] = None  # "настоящее время" / "Май 2023"
    duration: Optional[str] = None  # "3 года 6 месяцев"
    description: Optional[str] = None


class ResumeEducation(BaseModel):
    """Учебное заведение из резюме."""

    institution: Optional[str] = None
    faculty: Optional[str] = None
    level: Optional[str] = None  # "Неоконченное высшее" и т.п.
    year: Optional[str] = None  # год окончания / поступления — как в hh


class ResumeCourse(BaseModel):
    """Запись из «Повышение квалификации / Тесты, экзамены / Сертификаты»."""

    name: Optional[str] = None
    organization: Optional[str] = None  # «Stepik», «ТГ канал» и т.д.
    specialization: Optional[str] = None
    year: Optional[str] = None


class ResumeLanguage(BaseModel):
    name: str
    level: Optional[str] = None  # «Родной», «B2 — средне-продвинутый», ...


class ResumeRecommendation(BaseModel):
    organization: Optional[str] = None
    person: Optional[str] = None  # как написано: "Алексей (Начальник СБ)"


class ResumeDriving(BaseModel):
    has_own_car: bool = False
    categories: List[str] = Field(default_factory=list)  # ["B", "C"]


# ---------------------------------------------------------------------------
# Корневая модель
# ---------------------------------------------------------------------------


class Resume(BaseModel):
    """Распаршенное резюме hh.ru."""

    # — личные данные —
    full_name: Optional[str] = None
    gender: Optional[str] = None  # "Мужчина" / "Женщина"
    age: Optional[int] = None
    birthday: Optional[str] = None  # "12 ноября 2000"

    # — контакты —
    contacts: List[ResumeContact] = Field(default_factory=list)
    # Удобные «плоские» поля для совместимости / Stage-2 fraud-check:
    email: Optional[str] = None  # первый email из contacts
    phone: Optional[str] = None  # предпочитаемый телефон (или первый)

    # — локация / гражданство —
    location: Optional[str] = None  # «Проживает: …»
    citizenship: List[str] = Field(default_factory=list)
    work_permit: List[str] = Field(default_factory=list)
    relocation: Optional[str] = None  # «готов к переезду» / «не готов …»
    business_trips: Optional[str] = None

    # — должность и условия —
    title: Optional[str] = None  # «Backend-разработчик»
    salary: Optional[str] = None  # «100 000»
    currency: Optional[str] = None  # «₽»
    salary_kind: Optional[str] = None  # «на руки» / «до вычета налогов»
    specializations: List[str] = Field(default_factory=list)
    employment_type: Optional[str] = None  # «полная занятость»
    work_format: Optional[str] = None  # «на месте работодателя» / «удалённо»
    commute_time: Optional[str] = None  # «не имеет значения»

    # — опыт / навыки —
    summary: Optional[str] = None  # «Обо мне»
    skills: List[str] = Field(default_factory=list)
    total_experience: Optional[str] = None  # «3 года 6 месяцев»
    experience: List[ResumeExperience] = Field(default_factory=list)

    # — образование и доп. —
    education: List[ResumeEducation] = Field(default_factory=list)
    courses: List[ResumeCourse] = Field(default_factory=list)
    tests: List[ResumeCourse] = Field(default_factory=list)
    certificates: List[ResumeCourse] = Field(default_factory=list)
    languages: List[ResumeLanguage] = Field(default_factory=list)
    driving: Optional[ResumeDriving] = None
    recommendations: List[ResumeRecommendation] = Field(default_factory=list)

    # — метаданные —
    updated_at: Optional[str] = None  # «12 мая 2026 в 18:13»
    raw_text: Optional[str] = None  # полный plain-text дамп страницы
