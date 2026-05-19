"""
Простое JSON-хранилище истории диалогов.

Структура на диске:

    data/
        <user_id>/
            <dialog_id>.json   # {"messages": [{"role": ..., "content": ...}, ...]}

История внутри одного диалога — это уже готовый список messages в формате
OpenAI/DeepSeek (без system-сообщения; system всегда подставляется свежим
из резюме+шаблона на каждый вызов, чтобы оставаться неизменным префиксом
для кеша).

Для простоты примера используется файловый JSON. В проде стоит заменить
на Redis/Postgres/что угодно — интерфейс DialogStore сохраняется.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Iterable


Message = dict[str, str]  # {"role": "user"|"assistant", "content": "..."}


_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_id(value: str, kind: str) -> None:
    if not value or not _SAFE_ID.match(value):
        raise ValueError(
            f"Invalid {kind}: {value!r}. Allowed: letters, digits, dot, underscore, dash."
        )


class DialogStore:
    """Файловое JSON-хранилище диалогов, по одному файлу на (user_id, dialog_id)."""

    def __init__(self) -> None:
        self.root = Path(__file__).resolve().parent / "data" 
        self.root.mkdir(parents=True, exist_ok=True)
        # RLock, потому что append() держит лок и зовёт save(), который
        # тоже хочет залочиться — на обычном Lock это даст self-deadlock.
        self._lock = threading.RLock()

    def _path(self, user_id: str, dialog_id: str) -> Path:
        _validate_id(user_id, "user_id")
        _validate_id(dialog_id, "dialog_id")
        return self.root / user_id / f"{dialog_id}.json"

    def load(self, user_id: str, dialog_id: str) -> list[Message]:
        path = self._path(user_id, dialog_id)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        messages = data.get("messages", [])
        if not isinstance(messages, list):
            raise ValueError(f"Corrupted dialog file: {path}")
        return messages

    def save(self, user_id: str, dialog_id: str, messages: list[Message]) -> None:
        path = self._path(user_id, dialog_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with self._lock:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump({"messages": messages}, f, ensure_ascii=False, indent=2)
            tmp.replace(path)

    def append(
        self,
        user_id: str,
        dialog_id: str,
        new_messages: Iterable[Message],
    ) -> list[Message]:
        with self._lock:
            history = self.load(user_id, dialog_id)
            history.extend(new_messages)
            self.save(user_id, dialog_id, history)
            return history

    def reset(self, user_id: str, dialog_id: str) -> None:
        path = self._path(user_id, dialog_id)
        if path.exists():
            path.unlink()
