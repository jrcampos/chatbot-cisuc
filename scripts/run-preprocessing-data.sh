#!/usr/bin/env bash
set -euo pipefail

./scripts/run-ingestion.sh
./scripts/run-enhancement.sh
./scripts/run-embeddings.sh