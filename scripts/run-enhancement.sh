#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(
  cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd
)"

cd "$PROJECT_ROOT"

export CISUC_LOCAL_ROOT="${CISUC_LOCAL_ROOT:-$PROJECT_ROOT/.local}"

ENV_FILE="${CISUC_ENHANCEMENT_ENV_FILE:-$PROJECT_ROOT/secrets/enhancement.env}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Enhancement environment file not found: $ENV_FILE" >&2
  exit 2
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "OPENAI_API_KEY is not defined in $ENV_FILE" >&2
  exit 2
fi

mkdir -p \
  "$CISUC_LOCAL_ROOT/raw" \
  "$CISUC_LOCAL_ROOT/enriched" \
  "$CISUC_LOCAL_ROOT/chroma" \
  "$CISUC_LOCAL_ROOT/logs"

python -m preprocessing.enhancement.enhancement "$@"