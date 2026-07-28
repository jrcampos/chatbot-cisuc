#!/usr/bin/env bash
set -euo pipefail

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