#!/usr/bin/env bash
set -euo pipefail

set -a
source config/chatbot-common.env
source config/preprocessing.env
set +a

: "${CHROMADB_FINAL_IMAGE:?CHROMADB_FINAL_IMAGE must be defined}"

CHROMA_CONTAINER="$(
  docker compose \
    -f preprocessing/docker-compose.yaml \
    ps -q chromadb
)"

if [[ -z "$CHROMA_CONTAINER" ]]; then
  echo "Chroma container not found." >&2
  exit 1
fi

# Stop Chroma so its database is flushed.
docker compose \
  -f preprocessing/docker-compose.yaml \
  stop chromadb

# Create the populated Chroma image from the stopped container.
docker commit \
  "$CHROMA_CONTAINER" \
  "$CHROMADB_FINAL_IMAGE"

echo "Created populated Chroma image: $CHROMADB_FINAL_IMAGE"