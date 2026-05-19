"""
Демо-сценарий:

Один пользователь "ivan" ведёт диалог "search-2026-q2" и подряд кидает
три разные вакансии. Ожидание:

  - 1-й запрос: cache miss (префикс ещё не закеширован).
  - 2-й и 3-й запросы: cache hit на system + предыдущие пары
    (multi-turn cache, см. https://api-docs.deepseek.com/guides/kv_cache).

Запуск:
    cp .env.example .env  # вписать DEEPSEEK_API_KEY
    pip install -r requirements.txt
    python -m examples.run
"""

from __future__ import annotations

import os
from pathlib import Path

from app.config import DEEPSEEK_API_KEY
from app.services.cover_letter.service import CoverLetterService


async def generate_cover_letter(user_id: str, resume: str, vacancy: str) -> str:
    if not DEEPSEEK_API_KEY:
        raise SystemExit(
            "DEEPSEEK_API_KEY не задан. Скопируйте .env.example в .env и впишите ключ."
        )

    service = CoverLetterService(resume=resume)
    dialog_id = "chat"

    #for i, vacancy in enumerate(VACANCIES, 1):
    #    print(f"\n==================== Запрос #{i} ====================")
    #    result = service.generate(user_id, dialog_id, vacancy)
    #    s = result.stats
    #    print(
    #        f"[usage] prompt={s.prompt_tokens} "
    #        f"(cache_hit={s.cache_hit_tokens}, cache_miss={s.cache_miss_tokens}, "
    #        f"hit_ratio={s.hit_ratio:.0%}) "
    #        f"completion={s.completion_tokens} "
    #        f"history_len={result.history_len}"
    #    )
    #    print("---- Сопроводительное письмо ----")
    #    print(result.cover_letter)
    
    result = service.generate(user_id, dialog_id, vacancy)
    if result.history_len > 100:

        user_path = service._path / f"{user_id}"
        
        path_current_file = f'{user_path}/{dialog_id}'
        i = len(list(user_path.iterdir()))
        now_chat = Path(path_current_file + ".json")
        now_chat.rename(path_current_file + f'_{i}.json')

        file_path = Path(path_current_file + ".json").write_text("{}")
    return result.cover_letter
