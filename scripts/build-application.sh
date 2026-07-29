#!/usr/bin/env bash
set -euo pipefail

# Preserve values explicitly provided by preview deployment or CI.
PROVIDED_VITE_ENDPOINT="${VITE_ORCHESTRATOR_API_ENDPOINT:-}"

set -a
source config/chatbot-common.env
source config/chatbot.env
set +a

# If local, source secrets; otherwise they come from CI.
if [[ -f secrets/chatbot.env ]]; then
  set -a
  source secrets/chatbot-common.env
  source secrets/chatbot.env
  set +a
fi

# Explicit environment values take precedence over local files.
if [[ -n "$PROVIDED_VITE_ENDPOINT" ]]; then
  export VITE_ORCHESTRATOR_API_ENDPOINT="$PROVIDED_VITE_ENDPOINT"
fi

: "${CHROMADB_FINAL_IMAGE:?CHROMADB_FINAL_IMAGE is missing or empty}"
: "${VITE_ORCHESTRATOR_API_ENDPOINT:?VITE_ORCHESTRATOR_API_ENDPOINT is missing or empty}"
: "${OPENAI_API_KEY:?OPENAI_API_KEY is missing or empty}"
: "${OLLAMA_URL:?OLLAMA_URL is missing or empty}"

COMPOSE_ARGS=(-f application/docker-compose.yaml)

while (($#)); do
  COMPOSE_ARGS+=(-f "$1")
  shift
done

echo "GUI API endpoint: $VITE_ORCHESTRATOR_API_ENDPOINT"

if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
  docker compose \
    -p "$COMPOSE_PROJECT_NAME" \
    "${COMPOSE_ARGS[@]}" \
    up -d --build
else
  docker compose \
    "${COMPOSE_ARGS[@]}" \
    up -d --build
fi