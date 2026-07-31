#!/usr/bin/env bash
# Bring up a local Postgres and apply the schema. Idempotent.
set -euo pipefail
PGBIN=/usr/lib/postgresql/16/bin
PGDATA=${PGDATA:-/var/lib/pgdata}
DB=${DB:-autoresearch}

id postgres >/dev/null 2>&1 || useradd -m postgres
mkdir -p "$PGDATA" /var/run/postgresql
chown postgres:postgres "$PGDATA" /var/run/postgresql

[ -f "$PGDATA/PG_VERSION" ] || su postgres -c "$PGBIN/initdb -D $PGDATA -A trust" >/dev/null
su postgres -c "$PGBIN/pg_ctl -D $PGDATA -l /tmp/pg.log -o '-k /var/run/postgresql' status" >/dev/null 2>&1 \
  || su postgres -c "$PGBIN/pg_ctl -D $PGDATA -l /tmp/pg.log -o '-k /var/run/postgresql' -w start" >/dev/null

su postgres -c "$PGBIN/createdb -h /var/run/postgresql $DB" 2>/dev/null || true
psql -h /var/run/postgresql -U postgres -d "$DB" -qtc \
  "select 1 from information_schema.tables where table_name='campaign'" | grep -q 1 \
  || psql -h /var/run/postgresql -U postgres -d "$DB" -q -v ON_ERROR_STOP=1 \
       -f "$(dirname "$0")/../migrations/001_initial.sql"

echo "ready: host=/var/run/postgresql user=postgres dbname=$DB"
