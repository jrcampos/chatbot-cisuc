#!/usr/bin/env bash
set -euo pipefail

set -a
source config/chatbot-common.env
source config/preprocessing.env
set +a

./scripts/run-ingestion.sh
./scripts/run-enhancement.sh --limit 3
./scripts/run-embeddings.sh