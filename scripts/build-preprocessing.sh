#!/usr/bin/env bash
set -euo pipefail

docker compose \
  -f preprocessing/docker-compose.yaml \
  up -d --build \
  chromadb \
  preprocessing