#!/usr/bin/env bash
# Runs once, on an empty data directory (docker-entrypoint-initdb.d). Creates the read-only
# group role and the chatbot's login. Values are passed as psql variables and quoted by psql
# (:'var' / :"var"), never interpolated into SQL by the shell.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v reader_user="$EXTSRC_DB_READER_USER" -v reader_password="$EXTSRC_DB_READER_PASSWORD" \
  -v reader_timeout_ms="${EXTSRC_DB_READER_STATEMENT_TIMEOUT_MS:?EXTSRC_DB_READER_STATEMENT_TIMEOUT_MS must be defined}" \
  -v dbname="$POSTGRES_DB" <<'SQL'
SELECT 'CREATE ROLE extsrc_reader NOLOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'extsrc_reader') \gexec
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L IN ROLE extsrc_reader', :'reader_user', :'reader_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'reader_user') \gexec
SELECT format('ALTER ROLE %I SET default_transaction_read_only = on', :'reader_user') \gexec
SELECT format('ALTER ROLE %I SET statement_timeout = %L', :'reader_user', :'reader_timeout_ms') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO extsrc_reader', :'dbname') \gexec
SQL
