#!/usr/bin/env bash
set -euo pipefail

# Start the shared Traefik proxy.
docker compose \
    -p preview-proxy
    -f deployment/docker-compose.proxy.yaml \
    up -d

# Launch preview 42.
export PREVIEW_ID="42"
export COMPOSE_PROJECT_NAME="pr42"
export VITE_ORCHESTRATOR_API_ENDPOINT="/preview/42/chat"

./scripts/run-application.sh \
    up \
    application/docker-compose.preview.yaml

# Launch preview 43.
export PREVIEW_ID="43"
export COMPOSE_PROJECT_NAME="pr43"
export VITE_ORCHESTRATOR_API_ENDPOINT="/preview/43/chat"

./scripts/run-application.sh \
    up \
    application/docker-compose.preview.yaml