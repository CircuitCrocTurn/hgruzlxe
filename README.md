# Offerday

> **Кому это читать.** Этот README — точка входа для нового
> разработчика *и* для нового LLM-сессии: ниже описаны цель проекта,
> доменная модель и точки роста. Не нужно «жёстко анализировать
> концепцию» — она уже описана здесь, ссылайся на этот файл и на
> `docs/`.

## Что это вообще

Offerday — личный кабинет соискателя, который **автоматизирует
рутину поиска работы**. С точки зрения пользователя это один
дашборд, в котором:

* отклики на вакансии (`/responses`) — отклики автоматически
  отправляются от его имени на job-площадках, история и статусы видны
  в едином списке;
* чаты (`/chats`) — все диалоги с работодателями стянуты в одно
  место, можно читать и отвечать прямо из нашего интерфейса;
* статистика (`/dashboard`) — сколько откликов, отказов,
  приглашений за период;
* настройки (`/settings`) — резюме, подключения к площадкам,
  каналы уведомлений (email / телеграм).

Внутри это **FastAPI-приложение + PostgreSQL + асинхронные
job-site-клиенты**, которые ходят на внешние сервисы (hh.ru,
career.habr.com) под кукой пользователя, нормализуют ответы и
складывают в нашу БД. Дашборд читает только нашу БД и работает,
даже если внешняя площадка прилегла.

Поддерживаемые площадки:

| Площадка             | Логин                                | Отклики | Чаты              |
| -------------------- | ------------------------------------ | ------- | ----------------- |
| `hh.ru`              | OTP по телефону через SMS + 2captcha | да      | да (через chatik) |
| `career.habr.com`    | email + пароль + temp.coda.ink       | да      | в работе, см. `docs/habr_chats_integration.md` |

## Стек

* **Backend.** Python 3.11+, FastAPI, Uvicorn, Jinja2.
* **БД.** PostgreSQL только (asyncpg-драйвер; SQLite не
  поддерживается, `DATABASE_URL` должен начинаться с
  `postgresql+asyncpg://`). Схема — SQLAlchemy 2.0 declarative,
  миграции — Alembic (async-aware `env.py`).
* **Сессии.** Подписанные cookie `offerday_session`
  (Itsdangerous). Сессия площадки хранится в БД в
  `platform_credentials.encrypted_session` (Fernet-шифрование от
  `SESSION_SECRET`).
* **HTTP-клиенты к площадкам.** `aiohttp` поверх общего
  `BaseJobSiteClient` (контекст-менеджер: грузит cookies из БД при
  входе, сохраняет обратно при выходе). См. `app/services/job_sites/`.
* **Captcha.** 2captcha (`TWOCAPTCHA_KEY` в `.env`) — только для hh.ru.
* **Frontend.** Никакого SPA: серверный рендер Jinja-шаблонов
  (`app/templates/`) + минимум JS прямо в шаблонах (`fetch()` на API
  эндпойнты ниже).
* **Воркеры.** Шедулер опроса откликов / чатов поднимается из
  `app.queue.scheduler` (см. `app/queue/`).

## Раскладка

```
app/
  main.py                          FastAPI app & page routes (landing, login, dashboard, …)
  config.py                        env-driven settings (DATABASE_URL, SESSION_SECRET, TWOCAPTCHA_KEY, …)
  api/
    __init__.py                    регистрация роутеров
    auth.py                        /auth/request-code, /auth/verify-code, /auth/logout
    dashboard.py                   /api/* — всё что дёргает фронт дешборда
  services/
    auth.py                        helper'ы поверх hh-логина
    platform_password.py           детерминированный пароль для площадок
    temp_mail.py                   temp.coda.ink: создание ящика, чтение писем
    job_sites/
      base.py                      BaseJobSiteClient (общий aiohttp + БД cookies)
      hh/
        __init__.py                HHClient (OTP login, 2captcha, отклики, чатик)
        chats_sync.py              hh.ru → таблицы chats / messages
        ...
      habr/
        __init__.py                HabrClient (логин, онбординг, отклики на вакансии)
        auth_flow.py               двух-шаговый коннект habr из /settings (start/verify/cancel)
        vacancy_extractor.py       парсинг страницы вакансии
        chats_sync.py              ← планируемый модуль (см. docs/habr_chats_integration.md)
  schemas/                         Pydantic-модели (resume и др.)
  db/
    base.py, session.py            SQLAlchemy 2.0 async engine + AsyncSession + session_scope()
    users.py                       ensure_user_for_session, create_user_for_phone, …
    dashboard_data.py              ВСЁ что читает дешборд: list_chat_cards,
                                     list_response_rows, dashboard_stats, …
    models/                        User, Subscription, Resume,
                                     PlatformCredential, ActionLog, JobRun,
                                     VacancyResponse,
                                     Chat, Message,
                                     CoverLetterTemplate, IdentityCheck,
                                     UserPreferences, ResumeJob
  templates/                       Jinja: landing.html, login.html,
                                     dashboard.html, responses.html,
                                     chats.html, settings.html, tariffs.html
  static/                          локальные шрифты Inter/JetBrains Mono + Phosphor + логотипы компаний
  queue/                           шедулер периодических задач
alembic/                           миграции + async env.py
alembic.ini
requirements.txt
.env.example
docs/
  habr_chats_integration.md        ← разбор парсинга чатов career.habr.com
```

## Запуск

PostgreSQL нужен живой — SQLite-фолбэка нет. Самый быстрый путь —
бандлд `docker-compose.yml` с Postgres 16 и кредами из `.env.example`.

1. Поднять БД + создать `.env`:
   ```sh
   docker compose up -d db
   cp .env.example .env          # отредактировать TWOCAPTCHA_KEY / SESSION_SECRET
   ```
2. Виртуалка и зависимости:
   ```sh
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Миграции:
   ```sh
   alembic upgrade head
   ```
4. Сервер:
   ```sh
   uvicorn app.main:app --reload --port 8000
   ```
   Или одной командой (делает шаги 2–4):
   ```sh
   ./run.sh
   ```
5. Открыть http://localhost:8000 — лендинг.
   `/login` — телефон → SMS → редирект в `/dashboard`.
   Все 4 страницы кабинета (`/dashboard`, `/responses`, `/chats`,
   `/settings`) требуют сессию; без неё редирект на `/login`.

## Доменная модель

Главный жирный кусок — это `app/db/models/`. Ключевые сущности:

* **`User`** (`users`) — пользователь дешборда. Идентифицируется
  телефоном (E.164). Дочерние: `Subscription`, `PlatformCredential[]`,
  `Resume`, `VacancyResponse[]`, `Chat[]`, `ActionLog[]`, `JobRun[]`.
* **`PlatformCredential`** (`platform_credentials`) — учётка
  пользователя на конкретной площадке (`(user_id, service)`). Хранит
  Fernet-зашифрованную сессию (`encrypted_session`), статус
  (`active` / `expired` / …) и last-known login (телефон для hh,
  email для habr).
* **`Resume`** — резюме пользователя (используется для генерации
  cover letter и подкачки в habr).
* **`VacancyResponse`** — отклик на конкретную вакансию (площадка,
  external_id, статус, время).
* **`Chat`** (`chats`) — диалог с работодателем, скопированный с
  внешней площадки. Уникальность: `(user_id, service, external_id)`.
  Подробно — секция «Чаты» ниже.
* **`Message`** (`messages`) — сообщение в чате. Уникальность:
  `(chat_id, external_id)`.
* **`ActionLog`** — единая лента событий (отклик отправлен, чат
  обновлён, и т.п.), отображается в `/dashboard` как «История».
* **`JobRun`** — служебная запись о запуске воркера (нужна для
  идемпотентности и отчётности).

`app.db.dashboard_data` — единственная точка чтения для UI. Все
шаблоны и API дешборда дёргают функции отсюда (`list_chat_cards`,
`list_response_rows`, `dashboard_stats`, и т.д.), а не пишут SQL по
месту. Это даёт ровно один слой, который надо обновить при добавлении
новой колонки.

## Аутентификация пользователя в нашем приложении

1. `POST /auth/request-code  { phone }` → `HHClient.begin_otp_login`
   решает hh.ru-капчу через 2captcha; hh.ru шлёт SMS. **3–5 минут** —
   на фронте крутится лоадер всё это время.
2. `POST /auth/verify-code   { code }` → `HHClient.complete_otp_login`
   + `is_authenticated()`. Успех — ставим сессионную куку
   `offerday_session`, редирект на `/dashboard`.
3. `POST /auth/logout` — чистит сессионную куку (кнопка sign-out в
   сайдбаре). Кука hh.ru в БД **сохраняется**, чтобы следующий логин
   пропустил капчу. Чтобы реально выйти отовсюду — выставить
   `platform_credentials.status = 'expired'` (или удалить строку) для
   пары `(user_id, 'hh')`.

`app.db.users.ensure_user_for_session` — **read-only**: возвращает
existing `users` по номеру или `None`. Если `None` — на ручке
дашборда мы чистим cookie и редиректим в `/login`. Юзеры создаются
только в `/auth/verify-code` (через `create_user_for_phone`).
Никакого «silent auto-create» на dashboard-хитах.

## Сессии job-площадок

Каждая площадка имеет свой `*Client` (см. `app/services/job_sites/`),
все они унаследованы от `BaseJobSiteClient`. Контракт:

* `async with HHClient.from_context(user_id=..., hh_phone=...) as hh:`
  — открывает aiohttp-сессию, грузит cookies из
  `platform_credentials.encrypted_session`, ставит правильный
  User-Agent.
* На выходе сохраняет обновлённые cookies обратно в БД (площадки
  ротируют сессионные куки на каждом ответе).
* Все мутирующие методы атомарны и идемпотентны (есть
  `await hh.is_authenticated()` для предварительной проверки).

В hh.ru есть отдельный поддомен **`chatik.hh.ru`** — у него своя
кука и свои заголовки (`X-hhtmSource: chatik`). См.
`HHClient.chatik_headers` / `list_all_chats` / `mark_chat_read` /
`send_chat_message` / `get_chat_data`. Это шаблон, на который
ориентируется habr-интеграция (там аналогичный
`career.habr.com/api/frontend_v1/...`).

## Чаты

Чаты — отдельный домен поверх таблиц `chats` и `messages`. UI
(`/chats`, шаблон `chats.html`) **не знает про площадки** — он
рисует унифицированные карточки, которые отдаёт
`dashboard_data.list_chat_cards`.

### Модель `Chat`

`app/db/models/chat.py`. Ключевые поля:

```
service                str   "hh" | "habr"
external_id            str   id чата на площадке
title / subtitle       str   "Junior Python" / "Aston"
icon_url               str   логотип компании
link                   str   ссылка обратно на оригинал
unread_messages        int   счётчик
last_message_preview   str   plain-text (для карточки)
last_activity_at       dt    UTC, для сортировки
applicant_state        str   RESPONSE | INVITATION | DISCARD (hh-специфично)
last_message_outgoing  bool  «последнее сообщение — от меня?»
last_viewed_message_id str   для toggle_read
applicant_id           str   id/login кандидата на площадке
UNIQUE (user_id, service, external_id)
```

### Модель `Message`

```
external_id  str   id сообщения на площадке (может отсутствовать у системных)
text         str   plain-text (HTML мы НЕ храним — санитизируем при импорте)
author       str   "me" | "them" | "system"
author_name  str
is_read      bool
sent_at      dt    UTC
UNIQUE (chat_id, external_id)
INDEX (chat_id, sent_at)
```

### Поток данных (на примере hh.ru)

```
        ┌─────────────────────────────────────────────────────────┐
        │ /api/chats/sync (POST)                                  │
        └────────────────┬────────────────────────────────────────┘
                         │
              sync_hh_chats_for_user(user_id, hh_phone)
                         │
                 async with HHClient(...) as hh:
                         │
                 hh.list_all_chats() ── chatik.hh.ru/api/chats?page=1..N
                         │
              _build_chat_row → upsert в chats / messages
                         │
                 ActionLog "chats_sync_ok"
                         │
                 ┌───────┴───────┐
                 ▼               ▼
       list_chat_cards()   notify (telegram/email, если новые непрочитанные)
                 │
                 ▼
            chats.html   (полный SSR-рендер)
```

`POST /api/chats/{uuid}/refresh` — точечная дотяжка истории конкретного
чата (вызывается при открытии карточки). `POST /api/chats/{uuid}/read`
— ставит `unread_messages = 0` локально и зовёт `mark_chat_read` у
площадки. `POST /api/chats/{uuid}/send` — отправляет ответ через
клиент площадки + апсертит наше сообщение в `messages`.

### Habr-чаты

Не реализованы (на 17.05.2026). Полный разбор API
`career.habr.com/api/frontend_v1/chat/*`, маппинг на наши модели и
пошаговый план встраивания — в **`docs/habr_chats_integration.md`**.
Когда модуль `app/services/job_sites/habr/chats_sync.py` появится,
он подцепится к существующему `/api/chats/sync` (см. план в docs)
без новых ручек.

## База данных

Схема живёт в `app/db/models/*.py` (SQLAlchemy 2.0 declarative).
Весь рантайм-код ходит через async-сессии (`AsyncSession`,
`create_async_engine`, `asyncpg`). PostgreSQL — единственный
поддерживаемый backend; `DATABASE_URL` должен быть
`postgresql+asyncpg://...` (иначе приложение упадёт на старте).

Миграции — Alembic:

```sh
# Применить накопленные миграции
alembic upgrade head

# После правок в app/db/models/ — сгенерировать новую миграцию
alembic revision --autogenerate -m "<короткое описание>"
alembic upgrade head

# Откатить последнюю миграцию, если жалеешь
alembic downgrade -1
```

`alembic/env.py` сам подтягивает `DATABASE_URL` из окружения и
использует async-engine, поэтому миграции бегут против того же
Postgres, что и приложение. `app.main:lifespan` **не** делает
`Base.metadata.create_all` — схемой владеет Alembic.

### Async-сессия в коде

В FastAPI-ручках — через DI `app.db.session.get_db`:

```python
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db

@router.get("/things")
async def list_things(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Thing))).scalars().all()
    return rows
```

`get_db` открывает одну транзакцию на запрос (commit на успехе,
rollback на исключении). В воркерах/скриптах вне FastAPI — async
context-manager `session_scope()` из того же модуля.

## API ручек дашборда (важные)

Все живут в `app/api/dashboard.py` и требуют валидную сессию.

| Ручка                                | Что делает                                                              |
| ------------------------------------ | ----------------------------------------------------------------------- |
| `POST /api/chats/sync`               | Полная синхронизация чатов всех подключённых площадок. Долгая (несколько секунд) — фронт показывает спиннер. |
| `GET  /api/chats/{uuid}`             | Полный объект чата с историей сообщений (для рендера карточки).         |
| `POST /api/chats/{uuid}/refresh`     | Тяжёлая дотяжка истории отдельного чата (специально отделено от GET, чтобы клик «обновить» был явным). |
| `POST /api/chats/{uuid}/read`        | Локально + у площадки: пометить прочитанным.                            |
| `POST /api/chats/{uuid}/send`        | Отправить ответ. Площадка → апсерт в `messages` → возврат обновлённой карточки. |
| `POST /api/responses/sync`           | Аналог `chats/sync` для откликов.                                       |
| `POST /api/settings/platforms/habr/connect` | Двух-шаговое подключение habr (см. `auth_flow.start_habr_connect_flow`).|
| `POST /api/settings/platforms/habr/verify`  | Принять код подтверждения из почтового ящика на temp.coda.ink.          |

## Заглушки и точки роста

* `app/schemas/resume.py` — минимальная Pydantic-модель Resume
  (full_name, raw_text, …). Поля под реального парсера дописываем по
  мере необходимости.
* `app/services/job_sites/hh/resume_parser.py` — `resume_parse(text)`
  пока ловит только plain-text + лучшее-усилие по имени. Тут место
  для нормального резюме-парсера.
* `app/services/job_sites/habr/chats_sync.py` — **не существует**,
  см. `docs/habr_chats_integration.md`.
* WebSocket к `wss://pusher.habr.com:8443/ws/career` (live-обновления
  habr-чатов) — нужно потом, MVP закрывается polling-синком.

## Подводные камни

* **2captcha платный и медленный.** Без `TWOCAPTCHA_KEY` ручка
  `/auth/request-code` отдаёт 500.
* **DataDome на hh.ru.** `HHClient` ходит на hh.ru обычным
  User-Agent'ом. Если внезапно прилетел `hh_blocked` из
  `/auth/request-code` — в `hh.py:fetch_html` есть комментарий про
  переход на `curl_cffi`.
* **Habr ротирует сессию и CSRF на каждом ответе.** Всегда обнимай
  цепочку запросов одним `async with HabrClient(...)` (иначе
  потеряешь свежую куку и поймаешь 401 на следующем POST).
* **`Sec-WebSocket-Protocol = JWT`** — нестандартный приём, но
  `pusher.habr.com` его требует. В `aiohttp` это
  `ws_connect(url, protocols=[jwt])`.

## Куда смотреть в первую очередь, если ты новый LLM

1. `app/db/models/` — понять, какие сущности живут.
2. `app/db/dashboard_data.py` — единственная точка чтения для UI;
   там видно, что показывается в каждой странице.
3. `app/api/dashboard.py` — все ручки, которые дёргает фронт.
4. `app/services/job_sites/{hh,habr}/__init__.py` — клиенты к
   площадкам (паттерн `BaseJobSiteClient`, чатиковые / отклики).
5. `app/services/job_sites/hh/chats_sync.py` — канонический образец,
   как синхронизировать чаты площадки в наши таблицы. Habr должен
   быть зеркалом.
6. `docs/habr_chats_integration.md` — план интеграции habr-чатов.
