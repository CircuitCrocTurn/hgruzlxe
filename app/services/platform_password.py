"""Детерминированный «платформенный» пароль пользователя.

Пароль производится из ``user.id`` (UUID) + глобальной соли
:data:`app.config.PLATFORM_PASSWORD_SALT` через SHA-256. То есть:

* В БД пароль НЕ хранится — его всегда можно пересчитать по ``user.id``.
* Один и тот же ``user.id`` всегда даёт один и тот же пароль (пока соль
  не меняется). Это важно, потому что мы используем этот пароль при
  регистрации аккаунтов на job-площадках от имени пользователя
  (habr, и т.д.), и показываем тот же пароль самому пользователю в
  ``/settings`` для ручного входа.
* Ротация соли == «отзыв» всех платформенных паролей сразу.

Формат вывода: 16 символов, ``[A-Za-z0-9]``. Этого хватает по
требованиям большинства площадок (habr — минимум 8, заглавные/цифры);
гарантия наличия и буквы, и цифры обеспечивается коротким циклом
коррекции в конце функции.
"""
from __future__ import annotations

import base64
import hashlib
import uuid

from app.config import PLATFORM_PASSWORD_SALT


# Длина итогового пароля. 16 — компромисс между «помещается в SMS»
# (нерелевантно, мы его не шлём) и «не выглядит игрушечным».
_PASSWORD_LENGTH = 16


def generate_platform_password(user_id: uuid.UUID | str) -> str:
    """Вернуть детерминированный пароль для пользователя.

    Алгоритм:
        1. ``digest = sha256(f"{user_id}:{salt}")``
        2. ``b64 = base64.urlsafe_b64encode(digest)`` — отбрасываем
           padding и небуквенно-цифровые символы (``-``, ``_``).
        3. Берём первые 16 символов.
        4. Если в первой выборке нет хотя бы одной цифры/буквы —
           добавляем недостающую категорию из «запасной части»
           base64-строки. На SHA-256 это крайне маловероятный путь,
           но на 1 user.id всё же возможно.
    """
    raw = f"{user_id}:{PLATFORM_PASSWORD_SALT}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()
    # url-safe base64 без padding: ровно 43 символа из [A-Za-z0-9_-]
    encoded = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    alnum = "".join(c for c in encoded if c.isalnum())

    pwd = alnum[:_PASSWORD_LENGTH]
    if len(pwd) < _PASSWORD_LENGTH:
        # Маловероятно (alnum обычно ~38–40 символов из 43), но
        # для устойчивости — добиваем фиксированным паттерном.
        pwd = (pwd + "0123456789abcdef")[:_PASSWORD_LENGTH]

    has_digit = any(c.isdigit() for c in pwd)
    has_alpha = any(c.isalpha() for c in pwd)
    if not has_digit or not has_alpha:
        # «Залатать» категорию, не нарушая детерминированности:
        # ищем подходящий символ в «остатке» alnum после первых 16.
        tail = alnum[_PASSWORD_LENGTH:]
        replacement = ""
        if not has_digit:
            replacement = next((c for c in tail if c.isdigit()), "7")
        elif not has_alpha:
            replacement = next((c for c in tail if c.isalpha()), "a")
        # Меняем последний символ — детерминированно.
        pwd = pwd[:-1] + replacement

    return pwd
