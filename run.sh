#!/usr/bin/env bash
# One-touch launcher (без Docker).
#
# Что делает:
#  1. Создаёт venv `.venv` и ставит зависимости (только если ещё нет).
#  2. Грузит `.env` (или копирует из `.env.example`).
#  3. Бутстрапит локальный Postgres: создаёт юзера/БД через `sudo -u postgres`,
#     если их ещё нет (требует одноразово sudo при первом запуске).
#  4. Если в `alembic/versions/` пусто — делает `alembic revision --autogenerate`
#     по текущим моделям (одна миграция «init»). Дальше — `alembic upgrade head`.
#  5. Стартует uvicorn → lifespan дёрнет `reset_stuck_jobs()` и `start_scheduler()`.
#
# Идемпотентно: повторный запуск не пересоздаёт БД, не плодит миграции,
# не качает зависимости заново.
set -euo pipefail

cd "$(dirname "$0")"

# ─── 1. venv ──────────────────────────────────────────────────────────
if [ ! -d .venv ]; then
    echo "[setup] creating virtualenv .venv …"
    python3 -m venv .venv
fi
# shellcheck disable=SC1091
. .venv/bin/activate

if [ ! -f .venv/.deps_installed ] || [ requirements.txt -nt .venv/.deps_installed ]; then
    echo "[setup] installing dependencies …"
    pip install -q --upgrade pip
    pip install -q -r requirements.txt
    touch .venv/.deps_installed
fi

# ─── 2. .env ──────────────────────────────────────────────────────────
if [ ! -f .env ] && [ -f .env.example ]; then
    echo "[warn] no .env — copying .env.example. Edit it before logging in (TWOCAPTCHA_KEY, SESSION_SECRET)."
    cp .env.example .env
fi
if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

# ─── 3. Bootstrap локального Postgres ─────────────────────────────────
# Парсим DATABASE_URL вида:
#   postgresql+asyncpg://USER:PASS@HOST:PORT/DBNAME
# Если хост — localhost/127.0.0.1, проверяем что юзер+БД существуют,
# и создаём через `sudo -u postgres psql`, если нет.
DB_URL="${DATABASE_URL:-postgresql+asyncpg://offerday:offerday@127.0.0.1:5432/offerday}"

# вытащить части из URL — отсекаем драйверный префикс и креды
url_no_scheme="${DB_URL#*://}"
db_user="${url_no_scheme%%:*}"
rest="${url_no_scheme#*:}"
db_pass="${rest%%@*}"
host_port_db="${rest#*@}"
host_port="${host_port_db%%/*}"
db_host="${host_port%%:*}"
db_port="${host_port#*:}"
db_name="${host_port_db#*/}"
db_name="${db_name%%\?*}"   # на случай ?ssl=...

echo "[db] target: ${db_user}@${db_host}:${db_port}/${db_name}"

is_local=0
case "$db_host" in
    localhost|127.0.0.1|::1) is_local=1 ;;
esac

if [ "$is_local" = "1" ]; then
    if ! command -v psql >/dev/null 2>&1; then
        echo "[err] psql не найден. Установи postgres-client (Ubuntu: sudo apt install postgresql-client postgresql)."
        exit 1
    fi

    # Пробуем достучаться через целевого юзера (быстрая проверка, что
    # всё уже настроено — тогда sudo не понадобится).
    if PGPASSWORD="$db_pass" psql -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name" \
            -c 'SELECT 1' >/dev/null 2>&1; then
        echo "[db] connection OK"
    else
        echo "[db] юзер/БД ещё не созданы — создаю через sudo -u postgres (попросит пароль один раз)…"
        if ! command -v sudo >/dev/null 2>&1; then
            echo "[err] нет sudo. Создай вручную:"
            echo "      sudo -u postgres createuser -P ${db_user}"
            echo "      sudo -u postgres createdb -O ${db_user} ${db_name}"
            exit 1
        fi
        sudo -u postgres psql -v ON_ERROR_STOP=1 <<EOSQL
DO \$\$
BEGIN
   IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${db_user}') THEN
      CREATE ROLE ${db_user} LOGIN PASSWORD '${db_pass}';
   END IF;
END
\$\$;
EOSQL
        # CREATE DATABASE нельзя в транзакции / DO-блоке — отдельной командой
        if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${db_name}'" | grep -q 1; then
            sudo -u postgres createdb -O "${db_user}" "${db_name}"
        fi
        # Повторно проверяем.
        if ! PGPASSWORD="$db_pass" psql -h "$db_host" -p "$db_port" -U "$db_user" -d "$db_name" \
                -c 'SELECT 1' >/dev/null 2>&1; then
            echo "[err] подключиться по паролю всё равно не выходит."
            echo "      Проверь pg_hba.conf — должна быть строчка:"
            echo "      host all ${db_user} 127.0.0.1/32 scram-sha-256"
            exit 1
        fi
        echo "[db] БД и юзер созданы."
    fi
fi

# ─── 4. Миграции ──────────────────────────────────────────────────────
versions_dir="alembic/versions"
mkdir -p "$versions_dir"
# считаем только .py-файлы (без __init__.py)
have_versions=$(find "$versions_dir" -maxdepth 1 -type f -name '*.py' ! -name '__init__.py' | wc -l)

# Если в alembic/versions пусто — это «чистый» проект из zip-а. Нам
# нужно сгенерить миграцию по моделям, но сначала привести БД в
# консистентное состояние:
#  • если в alembic_version лежит ревизия от старого набора миграций
#    (которого больше нет на диске) — autogenerate упадёт на «can't
#    locate revision». Стираем версионный маркер.
#  • если в БД при этом уже есть таблицы (с предыдущих попыток),
#    autogenerate подумает «всё на месте» и нагенерит пустую миграцию.
#    Дропаем public-схему и пересоздаём — данных у нас всё равно нет.
if [ "$have_versions" -eq 0 ] && [ "$is_local" = "1" ]; then
    has_alembic_version=$(PGPASSWORD="$db_pass" psql -h "$db_host" -p "$db_port" \
        -U "$db_user" -d "$db_name" -tAc \
        "SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name='alembic_version'" 2>/dev/null || echo "")
    has_other_tables=$(PGPASSWORD="$db_pass" psql -h "$db_host" -p "$db_port" \
        -U "$db_user" -d "$db_name" -tAc \
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name <> 'alembic_version'" 2>/dev/null || echo 0)
    if [ "$has_alembic_version" = "1" ] || [ "${has_other_tables:-0}" -gt 0 ]; then
        echo "[db] обнаружена БД от предыдущих миграций → дропаю все таблицы в public (пустые, миграции пересоздадут)…"
        # На postgres 14 owner схемы public — ``postgres``, поэтому
        # ``DROP SCHEMA public`` от ``offerday`` падает «must be owner».
        # Дропаем таблицы по одной — их владельцем точно является
        # ``offerday``, потому что он их и создавал.
        PGPASSWORD="$db_pass" psql -h "$db_host" -p "$db_port" \
            -U "$db_user" -d "$db_name" -v ON_ERROR_STOP=1 -c "
DO \$\$
DECLARE r record;
BEGIN
  FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public'
  LOOP
    EXECUTE 'DROP TABLE IF EXISTS public.' || quote_ident(r.tablename) || ' CASCADE';
  END LOOP;
END
\$\$;
"
    fi
fi

if [ "$have_versions" -eq 0 ]; then
    echo "[db] alembic/versions пуст → генерирую начальную миграцию (autogenerate)…"
    alembic revision --autogenerate -m "init"
fi

echo "[db] applying migrations (alembic upgrade head) …"
alembic upgrade head

# ─── 5. uvicorn ───────────────────────────────────────────────────────
PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"
echo "[run] http://$HOST:$PORT"
exec uvicorn app.main:app --host "$HOST" --port "$PORT" "$@"
