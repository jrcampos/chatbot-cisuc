#!/usr/bin/env bash
set -euo pipefail

: "${PREVIEW_ID:?PREVIEW_ID is missing or empty}"
: "${COMPOSE_PROJECT_NAME:?COMPOSE_PROJECT_NAME is missing or empty}"
: "${VITE_ORCHESTRATOR_API_ENDPOINT:?VITE_ORCHESTRATOR_API_ENDPOINT is missing or empty}"

./scripts/run-application.sh up application/docker-compose.preview.yaml