#!/usr/bin/env bash
set -euo pipefail

set -a
source config/system.env
source config/preprocessing.env
set +a

docker compose \
  -f preprocessing/docker-compose.yaml \
  up -d --build \
  chromadb \
  preprocessing