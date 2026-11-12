#!/usr/bin/env bash
# One-shot load of the EU funding calls: collect -> enrich -> load
# every source (python -m external_sources.store run), then embed the new call records. The schema
# migrations run before, in the calls_migrate service. A degraded run (exit 1) keeps the last good
# data published; the exit code of the run is the container's exit code.
set -uo pipefail

python -m external_sources.store run --all --trigger deploy
code=$?
echo "[calls-ingest] run exit code ${code}"
python -m external_sources.store embed --records-only
echo "[calls-ingest] embed exit code $?"
python -m external_sources.store status || true
exit "${code}"
