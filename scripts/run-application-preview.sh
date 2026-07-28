#!/usr/bin/env bash
set -euo pipefail

# Start the shared Traefik proxy.
docker compose \
    -f deployment/docker-compose.proxy.yaml \
    up -d

# Launch preview 42.
PREVIEW_ID="42"
COMPOSE_PROJECT_NAME="pr42"
VITE_ORCHESTRATOR_API_ENDPOINT="/preview/42/chat"

export PREVIEW_ID
export COMPOSE_PROJECT_NAME
export VITE_ORCHESTRATOR_API_ENDPOINT

./scripts/run-application-preview-instance.sh

# Launch preview 43.
PREVIEW_ID="43"
COMPOSE_PROJECT_NAME="pr43"
VITE_ORCHESTRATOR_API_ENDPOINT="/preview/43/chat"

export PREVIEW_ID
export COMPOSE_PROJECT_NAME
export VITE_ORCHESTRATOR_API_ENDPOINT

./scripts/run-application-preview-instance.sh