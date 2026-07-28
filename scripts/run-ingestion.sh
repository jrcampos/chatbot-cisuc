#!/usr/bin/env bash
set -euo pipefail

set -a
source config/system.env
source config/preprocessing.env
set +a

if [[ -z "${CISUC_TOKEN:-}" ]]; then
  echo "Error: CISUC_TOKEN is missing or empty" >&2
  exit 1
fi

echo "CISUC_TOKEN is set; length=${#CISUC_TOKEN}"

# if local, source envs; otherwise they come from CI
if [[ -f secrets/preprocessing.env ]]; then
  set -a
  source secrets/chatbot-common.env
  source secrets/preprocessing.env
  set +a
fi

docker compose \
  -f preprocessing/docker-compose.yaml \
  exec \
  -e CISUC_TOKEN \
  preprocessing \
  python -m preprocessing.ingestion.cisuc_scraper.scraper_orchestrator "$@"