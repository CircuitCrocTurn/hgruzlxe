# Интеграция чатов career.habr.com

Цель документа — собрать всё, что нужно, чтобы повторить логику страницы
`career.habr.com/conversations` в нашем сайте: дотянуть `Chat` /
`Message` в БД, отрисовать в `/chats` и слать сообщения через тот же
бэкенд, что и hh.ru-чаты.

> **Статус: реализовано.** Все эндпойнты, описанные ниже, замонтированы и
> протестированы на HAR-трейсе и трёх HTML-снимках. Главные точки входа
> в коде:
>
> | Что | Где |
> | --- | --- |
> | HTTP-методы клиента (`_frontend_get_json`, `fetch_users_me`, `fetch_authenticity_token`, `fetch_conversations_html`, `fetch_chat_messages_page`, `toggle_chat_read_state`, `send_chat_message`) + `HabrAuthError` | `app/services/job_sites/habr/__init__.py` |
> | Devalue-парсер `__NUXT_DATA__`, маппинг `Chat`/`Message`, `sync_habr_chats_for_user`, `sync_habr_chat_messages_for_user`, `mark_habr_chat_read_for_user`, `send_habr_chat_message_for_user` | `app/services/job_sites/habr/chats_sync.py` |
> | Запуск habr-sync параллельно с hh-sync, ветвление по `chat.service` для `/refresh`, `/read`, `/messages`, поддержка `fetch_remote=True` в `build_active_chat_ctx` | `app/api/dashboard.py`, `app/db/dashboard_data.py` |
>
> Devalue-парсер прогнан на `chats1/2/3.html` — корректно извлекает
> `conversationsListWithSelectedLoginConversation[0]` с полями
> `login=dashaas`, `fullName=Дарья Сергейчик`, `companyName=Aston`,
> `subject={text, href}`, `unreadMessagesCount`, `lastMessage{id, body,
> createdAt, isMine, authorLogin, read, attachments}`.
>
> Что **не** входит в текущий PR (намеренно, чтобы не раздувать диф):
> WebSocket/Pusher live-апдейты (наш фронт и так дёргает `chats/sync`
> по лоаду), вложения (`chat_attachment_uuids` пробрасывается, но UI
> загрузки нет), `share_contacts`/`make_offer`/`job_invite`.

Источники данных, на которых построен разбор:

* HAR-архив `career.habr.com_Archive 26-05-17 17-31-47.har` — реальная
  сессия в Firefox: открытие списка диалогов, открытие диалога с
  `dashaas`, прочтение сообщений, отправка ответа «хорошо».
* Три «слепка» страницы (HTML, сохранённые подряд): `chats1.html`,
  `chats2.html`, `chats3.html` — три стадии одной и той же страницы
  `/conversations/dashaas` (см. ниже, какие именно).

Изначальное состояние кода (до этого PR) — в проекте уже была готовая
инфраструктура под чаты hh.ru (`app/services/job_sites/hh/chats_sync.py`,
`/api/chats/sync`, таблицы `chats` / `messages`, шаблон `chats.html`).
Habr-клиент (`app/services/job_sites/habr/__init__.py`) умел логиниться,
обходить онбординг и откликаться на вакансии — про чаты он ничего
не знал.

## 1. Три стадии страницы

Все три HTML-снапшота сохранены с одного URL — `/conversations/dashaas`.
Они отличаются состоянием правой панели сообщений и сайдбара. Это
позволяет понять, какие именно DOM-изменения отвечают каждому API-вызову
из HAR-файла.

### Stage 1 — первый рендер, есть непрочитанные

`chats1.html`:

* В сайдбаре карточка диалога `Дарья Сергейчик` помечена как активная
  (`router-link-exact-active conversation-card-link`).
* В правой панели — один блок `data-message-id="571137648"`
  (входящее сообщение от `dashaas`, кодовое имя `ASTON`).
* Перед ним стоит маркер-разделитель
  `<div data-unread-label="true" …>Непрочитанные сообщения</div>` —
  Vue-компонент рендерит его, пока чат на стороне сервера ещё не
  переведён в `read`.
* Под ним отдельный псевдомаркер с датой `17 мая 2026`.

### Stage 2 — после `toggle_read_state`

`chats2.html`:

* DOM сообщений — без изменений по сравнению со Stage 1 (тот же
  `571137648`, тот же `Непрочитанные сообщения`).
* Разница только на уровне backend-состояния (сервер ответил
  `{"hasBeenRead":true}` на `POST /chat/conversations/dashaas/toggle_read_state`).
  В сайдбаре пропадает счётчик `1` непрочитанных — но эту мутацию
  делает фронтенд локально, мы её в HTML не видим, потому что снапшот
  снят непосредственно после возврата API.
* Маркер `Непрочитанные сообщения` остаётся видимым до перезагрузки —
  это поведение Vue-компонента, а не серверного рендера: сервер знает,
  что прочитано, но UI продолжает показывать «границу» в текущей сессии.

### Stage 3 — после ответа «хорошо»

`chats3.html`:

* В правой панели уже **два** блока сообщений:
  * `571137648` — входящее (как было);
  * `571137664` — наше отправленное «хорошо».
* Маркера `Непрочитанные сообщения` больше нет — мы только что
  отправили сообщение, граница смысла не несёт.
* Дата-маркер `17 мая 2026` сохранился.

Вывод: единственный источник правды для UI — это API. HTML отдаёт
shell страницы и в `<div conversation="[object Object]" message="[object Object]" …>`
сериализует структуру сообщений; всё остальное (счётчики, статусы,
новые сообщения) приезжает по XHR / WebSocket.

## 2. Эндпойнты career.habr.com

Извлечено из HAR (`career.habr.com_Archive 26-05-17 17-31-47.har`,
138 entries; служебные `.css/.js/.woff/.png/effect.habr.com/sentry`
отфильтрованы). Порядок реальных запросов — снизу вверх по времени.

| #   | Метод | URL | Назначение |
| --- | ----- | --- | ---------- |
| 0   | GET   | `https://career.habr.com/conversations` | HTML-shell, отдаёт `<head>` + Vue-bootstrap. Тело — рендер с `<title>Диалог с пользователем Дарья Сергейчик – Хабр Карьера</title>` (302 KB). Полезной нагрузки по чатам в нём нет — список и сообщения подтягиваются XHR-ами ниже. |
| 55  | GET   | `…/api/frontend_v1/users/me` | `{"user":{"alias":"55zdxkor","fullName":"Дмитрий Костров","email":"55zdxkor@tt.coda.ink", …, "notificationCounters":{"messages":1, …}}, "meta":{"logoutToken":"…"}}`. Здесь же количество непрочитанных диалогов (бейдж в шапке). |
| 58  | GET   | `…/api/frontend_v1/featurer/check` | Фичефлаги; для чатов не нужны (304 Not Modified). |
| 63  | GET   | `…/api/frontend_v1/users/pusher_token` | JWT для WebSocket-пушера: `{"token":"eyJhbGciOiJIUzI1NiJ9…"}`. |
| 66  | GET   | `wss://pusher.habr.com:8443/ws/career` | WebSocket-апгрейд (101). `Sec-WebSocket-Protocol` = тот самый JWT. Канал `career`. Через него прилетают live-сообщения и счётчики. |
| 122 | GET   | `…/api/frontend_v1/notices/current` | Общие нотификации шапки (304). |
| 123 | POST  | `…/career-web-api/v1/page` | Тело `{"url":"https://career.habr.com/conversations/dashaas"}`. Ответ `{"ok":true}`. Это аналитический «ping» при смене страницы внутри SPA — можно не вызывать, на функциональность чатов он не влияет. |
| 128 | GET   | `…/api/frontend_v1/chat/messages?login=dashaas&page=1` | **Главный** эндпойнт чтения. Возвращает список `messages` для собеседника `dashaas` с пагинацией (см. формат ниже). |
| 131 | GET   | `…/api/frontend_v1/users/me` | Повтор после смены роута (счётчик `notificationCounters.messages` пересчитывается). |
| 132 | GET   | `…/api/frontend_v1/users/authenticity_token` | Свежий CSRF: `{"token":"IlY8cUAUwI2cayNI5ne…"}`. Habr ротирует токен на каждое мутирующее действие. |
| 134 | POST  | `…/api/frontend_v1/chat/conversations/dashaas/toggle_read_state` | Тело `{"new_state":"read"}`. Ответ `{"hasBeenRead":true}`. Помечает диалог прочитанным. |
| 136 | GET   | `…/api/frontend_v1/users/authenticity_token` | Очередной свежий CSRF перед отправкой сообщения. |
| 137 | POST  | `…/api/frontend_v1/chat/messages` | **Главный** эндпойнт записи. Тело `{"login":"dashaas","body":"хорошо\n","chatAttachmentUuids":[]}`. Ответ — обновлённый `messages[]` + `meta` (как в `GET /chat/messages`). |

Отдельно: в HAR нет запроса «получить список всех диалогов» — на момент
снятия лога мы уже сидели на конкретном `/conversations/dashaas`, а не
на корне `/conversations`. На корне обычно живёт
`GET /api/frontend_v1/chat/conversations?page=N` (см. ниже план — это
гипотеза, надо подтвердить на новой записи HAR). Альтернатива — пробить
тот же шаблон из HTML-shell корневой страницы.

### 2.1. Аутентификация

* Cookies: `_career_session` (HttpOnly, ротируется на каждом ответе —
  habr перевыпускает сессионный signed-cookie), `habr_uuid`,
  `qrator_msid2`, `mid`, `check_cookies`, `s61687262000a`, `s69676e616c`.
  Минимально-достаточный набор: `_career_session` + `habr_uuid` + `mid`.
* User-Agent: фиксируется на стороне habr — слать тот же UA, что
  использовали при логине; иначе сессия может «протухнуть» из-за
  fingerprint-проверок.
* CSRF: токен из `GET /api/frontend_v1/users/authenticity_token`, ходит
  в `x-csrf-token`. На GET-запросах не обязателен. На каждый POST берём
  свежий — habr иногда отвергает старый.
* Origin / Referer: `Origin: https://career.habr.com`,
  `Referer: https://career.habr.com/conversations/<login>`. Без них
  POST-ы могут отлететь на 401/403.
* WebSocket: токен из `users/pusher_token` подставляется в
  `Sec-WebSocket-Protocol`. Канал `wss://pusher.habr.com:8443/ws/career`.

### 2.2. Формат ответа `GET /chat/messages?login=…&page=N`

```json
{
  "messages": [
    {
      "id": 571137648,
      "createdAt": "2026-05-17 12:47:40",
      "body": "<p>Добрый день!<br>…</p>",
      "authorLogin": "dashaas",
      "isMine": false,
      "read": false,
      "kind": "message",
      "data": null,
      "attachments": [],
      "didAttachmentsDelete": false,
      "daysBeforeRemovalAttachments": null
    }
  ],
  "meta": {
    "totalResults": 1,
    "currentPage": 1,
    "totalPages": 1,
    "perPage": 20
  }
}
```

Поля:

* `id` — числовой id сообщения на habr. Сохраняем как строку в
  `messages.external_id` (как мы делаем для hh.ru).
* `createdAt` — `YYYY-MM-DD HH:MM:SS` **без таймзоны**. Эмпирически
  habr отдаёт MSK; на сохранении приводим к UTC (`Europe/Moscow` →
  `astimezone(UTC)`).
* `body` — HTML. Перед записью в `messages.text` либо сохраняем как
  есть и санитизируем на рендере, либо снимаем теги через `BeautifulSoup`.
  Для preview (`chats.last_message_preview`) точно нужен plain-text.
* `authorLogin` — slug пользователя; собеседник = `login`-параметр,
  поэтому `isMine = (authorLogin != login)` (см. ответ выше:
  входящее сообщение имеет `authorLogin == "dashaas"`, исходящее —
  `authorLogin == "55zdxkor"`, наш alias из `users/me`).
* `isMine` — habr уже посчитал за нас. Это и есть
  `last_message_outgoing` в нашей модели `Chat`.
* `read` — прочитано ли сообщение **получателем** (то есть для
  исходящих говорит «работодатель видел / не видел»). У hh.ru
  аналогичного поля нет — у нас в `Message` уже есть `is_read`, мапим
  напрямую.
* `kind` — `"message"` для обычных, `"system"` ожидаемо для системных
  (приглашения и т.п. — увидим, как только встретим в реальных диалогах).
* `attachments` — список UUID + meta. В первой итерации можно
  игнорировать; в `Message.text` подставить плейсхолдер «📎 файл».

### 2.3. Формат POST `/chat/messages`

Запрос:

```http
POST /api/frontend_v1/chat/messages HTTP/1.1
Origin: https://career.habr.com
Referer: https://career.habr.com/conversations/<login>
Content-Type: application/json
x-csrf-token: <token из users/authenticity_token>
Cookie: _career_session=…; habr_uuid=…; mid=…

{"login":"<login_собеседника>","body":"<HTML или plain>","chatAttachmentUuids":[]}
```

`body` принимает и plain-text (как в HAR: `"хорошо\n"`), и обёртку в
`<p>…</p>`. На стороне habr фронт оборачивает в `<p>` тривиальным
`marked`-like преобразованием — нам достаточно слать plain-text,
сервер сам прогонит через свой санитайзер.

Ответ — обновлённая страница `chat/messages?login=…&page=1`
(тот же payload, что у GET, но уже с нашим сообщением `571137664`).

### 2.4. POST `/chat/conversations/<login>/toggle_read_state`

```json
{"new_state": "read"}
```

→ `{"hasBeenRead": true}`. По названию параметра видно, что
`new_state` может быть и `"unread"` (для кнопки «отметить
непрочитанным» в Vue-меню). Это не критично для синхронизации.

## 3. Маппинг на наши модели

`app/db/models/chat.py` уже умеет habr — в файле есть `SERVICE_HABR = "habr"`.
Расширять схему **не нужно**, существующие поля закрывают всё:

| `Chat.*`                  | Источник на habr                                |
| ------------------------- | ----------------------------------------------- |
| `service`                 | константа `"habr"`                              |
| `external_id`             | login собеседника (`"dashaas"`) — habr именно по нему адресует чат, числового `chatId` у API нет |
| `title`                   | `fullName` собеседника (`"Дарья Сергейчик"`) — из `users/me`-like ответа `/api/frontend_v1/users/<login>` или из карточки в списке диалогов |
| `subtitle`                | название компании, если есть (`Aston`) — из той же карточки |
| `icon_url`                | `avatarUrl` собеседника                         |
| `link`                    | `https://career.habr.com/conversations/<login>` |
| `unread_messages`         | `messages.notReadCount` или счёт `messages[].isMine=false && read=false` |
| `last_message_preview`    | `striptags(messages[-1].body)[:300]`             |
| `last_activity_at`        | `messages[-1].createdAt` → UTC                  |
| `applicant_state`         | не отдаётся API напрямую — мы оставим `None` и в `is_chat_relevant` будем использовать только `last_message_outgoing` |
| `last_message_outgoing`   | `messages[-1].isMine`                           |
| `last_viewed_message_id`  | не отдаётся; для habr используем `last_message_id`, когда дёргаем `toggle_read_state` |
| `applicant_id`            | habr-логин (наш `alias`)                        |

| `Message.*`     | Источник на habr                                          |
| --------------- | --------------------------------------------------------- |
| `external_id`   | `messages[].id` → str                                     |
| `text`          | `striptags(messages[].body)` (или сам HTML — см. ниже)    |
| `author`        | `"me"` если `isMine`, иначе `"them"`; для `kind="system"` — `"system"` |
| `author_name`   | `messages[].authorLogin` → ник или, если есть профиль — `fullName` |
| `is_read`       | `messages[].read`                                         |
| `sent_at`       | `messages[].createdAt` → UTC                              |

`UniqueConstraint('user_id','service','external_id')` уже стоит на
`chats`, а на `messages` — `UniqueConstraint('chat_id','external_id')`,
так что upsert будет идентичен `_upsert_chat_and_last_message` из
`hh/chats_sync.py`.

Про HTML в `body`: для UI хочется и форматирование (ссылки,
переносы), и безопасность. Самый дешёвый вариант — хранить **plain
text** (`text` колонка `Text`), а перед рендером прогонять через
`bleach` или `markupsafe` в шаблоне. Это совпадает с тем, как
hh-чаты живут сейчас (`Message.text` уже plain). HTML-форматирование
рекрутёров встречается редко — потеря допустима в MVP.

## 4. Куда положить код

```
app/services/job_sites/habr/
  __init__.py              # уже есть, добавляем чат-методы в HabrClient
  chats_sync.py            # НОВЫЙ: зеркало hh/chats_sync.py
  vacancy_extractor.py     # как есть
  auth_flow.py             # как есть
  convert_page_to_post_data.py
```

### 4.1. Новые методы `HabrClient` (в `habr/__init__.py`)

```python
class HabrClient(BaseJobSiteClient):
    # ── Чаты career.habr.com ─────────────────────────────────────

    CHAT_BASE = "https://career.habr.com/api/frontend_v1"
    PUSHER_URL = "wss://pusher.habr.com:8443/ws/career"

    async def _xhr_headers(self, *, csrf: str | None = None) -> dict:
        h = {
            "Accept": "*/*",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Origin": BASE,
            "Referer": f"{BASE}/conversations",
            "X-Requested-With": "XMLHttpRequest",
        }
        if csrf is not None:
            h["x-csrf-token"] = csrf
        return h

    async def get_authenticity_token(self) -> str:
        url = f"{self.CHAT_BASE}/users/authenticity_token"
        async with self.session.get(url, headers=await self._xhr_headers()) as r:
            r.raise_for_status()
            data = await r.json()
        return data["token"]

    async def get_me(self) -> dict:
        url = f"{self.CHAT_BASE}/users/me"
        async with self.session.get(url, headers=await self._xhr_headers()) as r:
            r.raise_for_status()
            return await r.json()

    async def list_conversations(self, *, page: int = 1) -> dict:
        """GET /chat/conversations?page=N — список всех диалогов.

        ВНИМАНИЕ: точное имя endpoint'а взято по аналогии — в HAR его
        не было (мы открыли страницу уже на конкретном чате). При
        первой реальной интеграции пройти страницу /conversations с
        DevTools и при необходимости поправить URL/имена полей.
        """
        url = f"{self.CHAT_BASE}/chat/conversations"
        params = {"page": page}
        async with self.session.get(
            url, headers=await self._xhr_headers(), params=params
        ) as r:
            r.raise_for_status()
            return await r.json()

    async def get_chat_messages(self, *, login: str, page: int = 1) -> dict:
        url = f"{self.CHAT_BASE}/chat/messages"
        async with self.session.get(
            url,
            headers=await self._xhr_headers(),
            params={"login": login, "page": page},
        ) as r:
            r.raise_for_status()
            return await r.json()

    async def toggle_chat_read_state(
        self, *, login: str, new_state: str = "read",
    ) -> dict:
        csrf = await self.get_authenticity_token()
        url = (
            f"{self.CHAT_BASE}/chat/conversations/{login}/toggle_read_state"
        )
        async with self.session.post(
            url,
            headers={
                **await self._xhr_headers(csrf=csrf),
                "Content-Type": "application/json",
                "Referer": f"{BASE}/conversations/{login}",
            },
            json={"new_state": new_state},
        ) as r:
            r.raise_for_status()
            return await r.json()

    async def send_chat_message(
        self,
        *,
        login: str,
        body: str,
        attachment_uuids: list[str] | None = None,
    ) -> dict:
        csrf = await self.get_authenticity_token()
        url = f"{self.CHAT_BASE}/chat/messages"
        payload = {
            "login": login,
            "body": body,
            "chatAttachmentUuids": list(attachment_uuids or []),
        }
        async with self.session.post(
            url,
            headers={
                **await self._xhr_headers(csrf=csrf),
                "Content-Type": "application/json",
                "Referer": f"{BASE}/conversations/{login}",
            },
            json=payload,
        ) as r:
            r.raise_for_status()
            return await r.json()
```

### 4.2. `app/services/job_sites/habr/chats_sync.py`

Структурно — копия `hh/chats_sync.py`, но:

* `SERVICE_HABR = "habr"` вместо `SERVICE_HH = "hh"`.
* Маппинг `_build_chat_row` берёт поля из секции 3.
* `_extract_chat_data_messages` строится поверх `get_chat_messages`
  с пагинацией по `meta.currentPage`/`meta.totalPages`.
* Функции-точки входа на бэке (для воркера и `/api/chats/sync`):

```python
async def sync_habr_chats_for_user(
    *,
    user_id: uuid.UUID,
    habr_email: str | None = None,
) -> dict[str, int]:
    """Поднять HabrClient.from_context, обойти все диалоги, апсертнуть в БД."""

async def sync_habr_chat_messages_for_user(
    *,
    user_id: uuid.UUID,
    chat_id: uuid.UUID,
) -> dict[str, int]:
    """Тяжёлая дотяжка истории отдельного чата (для on-demand рендера)."""

async def mark_habr_chat_read_for_user(
    *,
    user_id: uuid.UUID,
    chat_id: uuid.UUID,
) -> None:
    """Зеркало mark_hh_chat_read_for_user."""

async def send_habr_chat_message_for_user(
    *,
    user_id: uuid.UUID,
    chat_id: uuid.UUID,
    text: str,
) -> Message:
    """Зеркало send_hh_chat_message_for_user."""
```

Все четыре используют `Chat.applicant_id` как `login` собеседника
(см. секцию 3 — это и есть наш ключ адресации на habr).

### 4.3. Изменения в API (`app/api/dashboard.py`)

Существующий `POST /api/chats/sync` ходит только в hh-синхронизатор
(см. `from app.services.job_sites.hh.chats_sync import sync_hh_chats_for_user`).
Нужно либо разнести на два эндпойнта, либо сделать диспетчер по
платформе.

Минимальный диф — добавить параллельный вызов:

```python
import asyncio
from app.services.job_sites.hh.chats_sync import sync_hh_chats_for_user
from app.services.job_sites.habr.chats_sync import sync_habr_chats_for_user

hh_stats, habr_stats = await asyncio.gather(
    sync_hh_chats_for_user(user_id=user.id, hh_phone=hh_phone),
    sync_habr_chats_for_user(user_id=user.id, habr_email=habr_email),
    return_exceptions=True,
)
```

Так фронт по-прежнему дёргает один POST, а данные приходят с обеих
площадок одновременно. Ошибку одной площадки логируем, а ответ
строим как union счётчиков (с префиксами `hh_*` / `habr_*`).

Для `POST /api/chats/<uuid>/refresh`, `…/read`, `…/send` уже есть
ветвление по `chat.service` (см. `chats_get_one` и
`chats_refresh_history`). Дописываем `elif chat.service == SERVICE_HABR`
рядом с существующим `if chat.service == SERVICE_HH`.

### 4.4. Шаблон `chats.html` и dashboard_data

`app/db/dashboard_data.py: list_chat_cards` уже агностичен к
платформе — он читает `chat.service`, `chat.link`, `chat.icon_url`,
`applicant_state`. Менять там, скорее всего, ничего не придётся.

В `chats.html` иконка платформы строится по `chat.service`; стоит
добавить ветку для `"habr"` (отдельный кружок с лого Хабр Карьеры).
Можно переиспользовать существующий `app/static/logotips/<uuid>.png`
через `download_company_logo`.

## 5. WebSocket (live-обновления) — отдельный пункт

`wss://pusher.habr.com:8443/ws/career` с JWT в
`Sec-WebSocket-Protocol`. После апгрейда habr-фронт получает push'ы
вида:

* новое сообщение → инкремент `notificationCounters.messages`
  и live-вставка в открытом чате;
* событие прочтения собеседником → обновление поля `read` на
  отправленных сообщениях.

Для MVP мы **не** тянем WebSocket — у нас уже есть пере-синхронизация
по polling (`sync_*_chats_for_user` запускается из шедулера + при
заходе на `/chats`). Это вполне закрывает 90% UX.

Если потом понадобится realtime — поднимать отдельный воркер,
который держит сокет, складирует события в очередь и в конце
триггерит точечную дотяжку конкретного чата (как мы это уже делаем
для hh-чатов через `sync_hh_chat_messages_for_user`).

## 6. Чек-лист на внедрение

1. [ ] Добавить методы из 4.1 в `HabrClient`.
2. [ ] Создать `app/services/job_sites/habr/chats_sync.py` по шаблону
   `hh/chats_sync.py`.
3. [ ] Расширить `/api/chats/sync` параллельным запросом к habr
   (см. 4.3).
4. [ ] В `chats_get_one` / `chats_refresh_history` / `chats_mark_read` /
   `chats_send` добавить ветку `service == "habr"`.
5. [ ] В `chats.html` подмешать иконку и линк на habr.
6. [ ] Прогнать руками: открыть habr-диалог, отметить прочитанным,
   ответить. Проверить, что после `sync_habr_chats_for_user` в БД
   появилась строка `chats.service='habr'` с правильным
   `applicant_id`.
7. [ ] Снять отдельный HAR на чистой `/conversations` (без открытого
   диалога), чтобы подтвердить точный URL и формат
   `GET /chat/conversations` (см. 4.1: предположение).

## 7. Известные риски

* Habr ротирует `_career_session` и CSRF на каждом ответе — наш
  `BaseJobSiteClient` уже сохраняет cookies в БД на `__aexit__`,
  поэтому достаточно делать сетевые вызовы внутри одного
  `async with HabrClient(...) as habr:` и не забывать в воркере
  перезагружать сессию между запусками.
* `Sec-WebSocket-Protocol` с JWT — нестандартное использование
  поля. В Python проще всего: `aiohttp` → `ws_connect(url, protocols=[jwt])`.
* Эндпойнт списка диалогов в HAR не виден — перед началом писать
  код стоит снять ещё один HAR на пустом `/conversations` и
  актуализировать раздел 4.1 (`list_conversations`).
* Habr не возвращает `applicantState`/`workflowTransition` —
  «отказ работодателя» нельзя определить по структурированному полю.
  Если нужно — пускать через тот же `is_refusal_text` из
  `hh/chats_sync.py`.
