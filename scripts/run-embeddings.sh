#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(
  cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
)"

cd "$PROJECT_ROOT"

export CISUC_LOCAL_ROOT="${CISUC_LOCAL_ROOT:-$PROJECT_ROOT/.local}"

ENV_FILE="${CISUC_EMBEDDINGS_ENV_FILE:-$PROJECT_ROOT/secrets/embeddings.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Embeddings environment file not found: $ENV_FILE" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ -z "${EMBEDDINGS_PROVIDER:-}" ]]; then
  echo "EMBEDDINGS_PROVIDER is not defined in $ENV_FILE" >&2
  exit 2
fi

if [[ "$EMBEDDINGS_PROVIDER" == "openai" ]] &&
   [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "OPENAI_API_KEY is not defined in $ENV_FILE" >&2
  exit 2
fi

mkdir -p \
  "$CISUC_LOCAL_ROOT/raw" \
  "$CISUC_LOCAL_ROOT/enriched" \
  "$CISUC_LOCAL_ROOT/chroma" \
  "$CISUC_LOCAL_ROOT/logs"

python -m preprocessing.embeddings.populate "$@"
