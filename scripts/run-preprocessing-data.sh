#!/usr/bin/env bash
set -euo pipefail

set -a
source config/chatbot-common.env
source config/preprocessing.env
set +a

./scripts/run-ingestion.sh
./scripts/run-enhancement.sh --limit 3


echo "===== Host .local ====="
pwd
find .local -maxdepth 3 -printf "%M %u:%g %p\n"

echo "===== Chroma container ====="
docker compose \
  -f preprocessing/docker-compose.yaml \
  exec chromadb sh -c '
    id

    echo "--- /data ---"
    ls -lah /data

    echo "--- permissions ---"
    find /data -maxdepth 2 -printf "%M %u:%g %p\n"

    echo "--- write test ---"
    touch /data/write-test
    echo ok >/data/write-test
    cat /data/write-test
    rm /data/write-test

    echo "--- sqlite ---"
    ls -lah /data/*.sqlite* 2>/dev/null || true
'

./scripts/run-embeddings.sh