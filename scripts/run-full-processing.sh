#!/usr/bin/env bash
set -euo pipefail

./scripts/build-preprocessing.sh
./scripts/run-ingestion.sh
./scripts/run-enhancement.sh "$@"
./scripts/run-embeddings.sh
./scripts/finish-preprocessing.sh