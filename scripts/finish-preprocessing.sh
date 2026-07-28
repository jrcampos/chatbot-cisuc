#!/usr/bin/env bash
set -euo pipefail

# Stop Chroma so its database is flushed.
docker compose \
  -f preprocessing/docker-compose.yaml \
  stop chromadb

# Build the populated Chroma image.
docker compose \
  -f preprocessing/docker-compose.yaml \
  build chromadb-final