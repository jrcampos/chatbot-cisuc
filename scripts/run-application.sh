#!/usr/bin/env bash
set -euo pipefail

set -a
source config/system.env
source config/application.env
set +a

# If local, source secrets; otherwise they come from CI.
if [[ -f secrets/application.env ]]; then
  set -a
  source secrets/chatbot-common.env
  source secrets/application.env
  set +a
fi

# Validate required configuration.
: "${CHROMADB_FINAL_IMAGE:?CHROMADB_FINAL_IMAGE is missing or empty}"
: "${VITE_ORCHESTRATOR_API_ENDPOINT:?VITE_ORCHESTRATOR_API_ENDPOINT is missing or empty}"

# If your backend services require these at runtime, keep them.
: "${OPENAI_API_KEY:?OPENAI_API_KEY is missing or empty}"
: "${LLM_URL:?LLM_URL is missing or empty}"

docker compose \
  -f application/docker-compose.yaml \
  up -d --build "$@"