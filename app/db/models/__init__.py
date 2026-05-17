"""Re-export всех моделей.

Импорт всех моделей здесь нужен по двум причинам:

1. Чтобы Alembic при автогенерации миграций видел все таблицы — для
   этого в `alembic/env.py` достаточно `from app.db.models import *`.
2. Чтобы строковые ссылки в `relationship("User", ...)` корректно
   разрешались — все классы должны быть импортированы до того, как
   SQLAlchemy финализирует mapper'ы.

По мере появления новых моделей (vacancy, application, conversation,
message, telegram_*, keyword_filter, vacancy_match) — добавляй сюда.
"""

from app.db.models.action_log import ActionLog
from app.db.models.chat import Chat
from app.db.models.job_run import JobRun
from app.db.models.message import Message
from app.db.models.platform_credential import PlatformCredential
from app.db.models.resume import Resume
from app.db.models.subscription import Subscription
from app.db.models.user import User
from app.db.models.vacancy_responses import VacancyResponse

__all__ = [
    "ActionLog",
    "Chat",
    "JobRun",
    "Message",
    "PlatformCredential",
    "Resume",
    "Subscription",
    "User",
    "VacancyResponse",
]
