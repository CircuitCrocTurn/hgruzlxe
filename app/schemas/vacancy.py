from dataclasses import dataclass
from typing import List

@dataclass
class Vacancy:
    title: str = ""
    company: str = ""
    salary: str = ""
    experience: str = ""
    employment: str = ""
    description: str = ""
    skills: List[str] = None
    link: str = ""
    # Полный URL логотипа компании, если его удалось вытащить со
    # страницы вакансии. Воркер скачает картинку в
    # ``app/static/logotips/<vacancy_response.id>.<ext>``.
    company_logo: str = ""

    def __post_init__(self):
        if self.skills is None:
            self.skills = []

    # --------------------- человеко-читаемый текст -------------------------
    def as_text(self) -> str:
        parts = [f"=== {self.title} ==="]
        if self.company:
            parts.append(f"Компания: {self.company}")
        if self.salary:
            parts.append(f"Зарплата: {self.salary}")
        else:
            parts.append("Зарплата: не указана")
        if self.experience:
            parts.append(f"Опыт: {self.experience}")
        if self.employment:
            parts.append(f"Занятость: {self.employment}")

        parts.append("")
        parts.append("--- Описание ---")
        parts.append(self.description or "(нет описания)")

        parts.append("")
        parts.append("--- Ключевые навыки ---")
        if self.skills:
            parts.extend(f"• {s}" for s in self.skills)
        else:
            parts.append("(не указаны)")

        return "\n".join(parts)