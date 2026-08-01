#!/usr/bin/env bash
set -euo pipefail

set -a
source config/chatbot-common.env
source config/preprocessing.env
set +a

echo "Building and starting preprocessing services..."
./scripts/build-preprocessing.sh

echo "Running ingestion..."
./scripts/run-ingestion.sh

echo "Running enrichment..."
./scripts/run-enhancement.sh "$@"

echo "Running embeddings..."
./scripts/run-embeddings.sh

echo "Creating populated Chroma image..."
./scripts/finish-preprocessing.sh