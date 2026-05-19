"""
Главный entry-point: CoverLetterService.

Идея:
- Резюме и шаблон кандидата загружаются один раз (на инстанс сервиса
  или на пользователя). Они формируют стабильный system-prompt.
- На каждый вызов generate(user_id, dialog_id, vacancy_text):
    1. Подгружаем историю диалога (user/assistant пары) из стора.
    2. Собираем messages = [system, ...history, user(vacancy)].
    3. Зовём DeepSeek.
    4. Сохраняем в историю user(vacancy) и assistant(letter).
    5. Возвращаем письмо + метрики кеша.

Что попадает в кеш DeepSeek:
- system-prompt одинаков между запросами одного пользователя →
  кешируется автоматически (на втором+ запросе).
- При продолжении одного и того же dialog_id весь предыдущий
  префикс (system + предыдущие пары) тоже кешируется (Example 1
  из доки DeepSeek про multi-turn).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.services.cover_letter.client import CacheStats, DeepSeekClient
from app.services.cover_letter.dialog_store import DialogStore, Message
from app.services.cover_letter.prompts import build_system_prompt, build_user_prompt


@dataclass
class GenerationResult:
    cover_letter: str
    stats: CacheStats
    history_len: int  # сколько messages в истории после этого вызова (без system)


class CoverLetterService:
    def __init__(
        self,
        resume: str,
        *,
        client: DeepSeekClient | None = None,
        store: DialogStore | None = None,
        temperature: float = 0.6,
        max_tokens: int | None = 2500,
    ) -> None:
        self._client = client or DeepSeekClient()
        self._store = store or DialogStore()
        self._path = DialogStore().root
        self._system_prompt = build_system_prompt(resume)
        self._temperature = temperature
        self._max_tokens = max_tokens

    @classmethod
    def from_files(
        cls,
        resume_path: str | Path,
        **kwargs,
    ) -> "CoverLetterService":
        resume = Path(resume_path).read_text(encoding="utf-8")
        return cls(resume=resume, **kwargs)

    def generate(
        self,
        user_id: str,
        dialog_id: str,
        vacancy_text: str,
    ) -> GenerationResult:
        history = self._store.load(user_id, dialog_id)

        user_msg: Message = {"role": "user", "content": build_user_prompt(vacancy_text)}
        messages = [
            {"role": "system", "content": self._system_prompt},
            *history,
            user_msg,
        ]

        result = self._client.chat(
            messages,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )

        assistant_msg: Message = {"role": "assistant", "content": result.content}
        new_history = self._store.append(user_id, dialog_id, [user_msg, assistant_msg])

        return GenerationResult(
            cover_letter=result.content,
            stats=result.stats,
            history_len=len(new_history),
        )

    def reset_dialog(self, user_id: str, dialog_id: str) -> None:
        self._store.reset(user_id, dialog_id)
