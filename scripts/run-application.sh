#!/usr/bin/env bash
set -euo pipefail

COMMAND="${1:?Usage: $0 <build|up|up-build|down> [compose override files...]}"
shift

case "$COMMAND" in
  build|up|up-build|down)
    ;;
  *)
    echo "Unknown command: $COMMAND" >&2
    echo "Usage: $0 <build|up|up-build|down> [compose override files...]" >&2
    exit 1
    ;;
esac

# Preserve values explicitly provided by preview deployment or CI.
PROVIDED_VITE_ENDPOINT="${VITE_ORCHESTRATOR_API_ENDPOINT:-}"

PROVIDED_CHROMADB_IMAGE="${CHROMADB_FINAL_IMAGE:-}"
PROVIDED_RAG_IMAGE="${RAG_IMAGE:-}"
PROVIDED_ORCHESTRATOR_IMAGE="${ORCHESTRATOR_IMAGE:-}"
PROVIDED_GUI_IMAGE="${GUI_IMAGE:-}"
PROVIDED_OPENAI_API_KEY="${OPENAI_API_KEY:-}"
PROVIDED_OLLAMA_URL="${OLLAMA_URL:-}"

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

if [[ -n "$PROVIDED_CHROMADB_IMAGE" ]]; then
  export CHROMADB_FINAL_IMAGE="$PROVIDED_CHROMADB_IMAGE"
fi

if [[ -n "$PROVIDED_RAG_IMAGE" ]]; then
  export RAG_IMAGE="$PROVIDED_RAG_IMAGE"
fi

if [[ -n "$PROVIDED_ORCHESTRATOR_IMAGE" ]]; then
  export ORCHESTRATOR_IMAGE="$PROVIDED_ORCHESTRATOR_IMAGE"
fi

if [[ -n "$PROVIDED_GUI_IMAGE" ]]; then
  export GUI_IMAGE="$PROVIDED_GUI_IMAGE"
fi

if [[ -n "$PROVIDED_OPENAI_API_KEY" ]]; then
  export OPENAI_API_KEY="$PROVIDED_OPENAI_API_KEY"
fi

if [[ -n "$PROVIDED_OLLAMA_URL" ]]; then
  export OLLAMA_URL="$PROVIDED_OLLAMA_URL"
fi

: "${CHROMADB_FINAL_IMAGE:?CHROMADB_FINAL_IMAGE is missing or empty}"
: "${RAG_IMAGE:?RAG_IMAGE is missing or empty}"
: "${ORCHESTRATOR_IMAGE:?ORCHESTRATOR_IMAGE is missing or empty}"
: "${GUI_IMAGE:?GUI_IMAGE is missing or empty}"
: "${VITE_ORCHESTRATOR_API_ENDPOINT:?VITE_ORCHESTRATOR_API_ENDPOINT is missing or empty}"
: "${OPENAI_API_KEY:?OPENAI_API_KEY is missing or empty}"
: "${OLLAMA_URL:?OLLAMA_URL is missing or empty}"

COMPOSE_ARGS=(-f application/docker-compose.yaml)

while (($#)); do
  COMPOSE_ARGS+=(-f "$1")
  shift
done

DOCKER_COMPOSE=(docker compose)

if [[ -n "${COMPOSE_PROJECT_NAME:-}" ]]; then
  DOCKER_COMPOSE+=(-p "$COMPOSE_PROJECT_NAME")
fi

DOCKER_COMPOSE+=("${COMPOSE_ARGS[@]}")

echo "Command: $COMMAND"
echo "GUI API endpoint: $VITE_ORCHESTRATOR_API_ENDPOINT"

case "$COMMAND" in
  build)
    "${DOCKER_COMPOSE[@]}" build
    ;;

  up)
    "${DOCKER_COMPOSE[@]}" up -d --pull always
    ;;

  up-build)
    "${DOCKER_COMPOSE[@]}" up -d --build
    ;;

  down)
    "${DOCKER_COMPOSE[@]}" down --remove-orphans --volumes
    ;;
esac