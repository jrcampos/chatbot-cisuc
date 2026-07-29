#!/usr/bin/env bash
set -euo pipefail

set -a
source config/chatbot-common.env
source config/preprocessing.env
set +a

REUSE_LOCAL_PREPROCESSING="${REUSE_LOCAL_PREPROCESSING:-false}"
LOCAL_DIR="${LOCAL_DIR:-.local}"

./scripts/build-preprocessing.sh

if [[ "$REUSE_LOCAL_PREPROCESSING" == "true" && -d "$LOCAL_DIR" ]]; then
    echo "Reusing preprocessing data from $LOCAL_DIR"
    echo "Skipping ingestion, enrichment, and embeddings."
else
    echo "Running ingestion..."
    ./scripts/run-ingestion.sh

    echo "Running enrichment..."
    ./scripts/run-enhancement.sh "$@"

    echo "Running embeddings..."
    ./scripts/run-embeddings.sh
fi

./scripts/finish-preprocessing.sh