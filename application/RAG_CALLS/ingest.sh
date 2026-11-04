#!/usr/bin/env bash
# Scheduled loader for the EU funding calls: migrate once, then every CALLS_INGEST_INTERVAL_SECONDS
# collect -> enrich -> load every source (python -m external_sources.store run) and embed the new
# call records. A degraded run (exit 1) keeps the last good data published; it is logged, not fatal.
set -uo pipefail

python -m external_sources.store migrate || exit $?

while true; do
  python -m external_sources.store run --all --trigger schedule
  echo "[calls-ingest] run exit code $?"
  python -m external_sources.store embed --records-only
  echo "[calls-ingest] embed exit code $?"
  python -m external_sources.store status || true
  sleep "${CALLS_INGEST_INTERVAL_SECONDS:?CALLS_INGEST_INTERVAL_SECONDS must be defined}"
done
