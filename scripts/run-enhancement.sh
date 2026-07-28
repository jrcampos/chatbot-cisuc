#!/usr/bin/env bash
set -euo pipefail

set -a
source config/system.env
source config/preprocessing.env
set +a

# if local, source envs; otherwise they come from CI
if [[ -f secrets/preprocessing.env ]]; then
  set -a
  source secrets/chatbot-common.env
  source secrets/preprocessing.env
  set +a
fi

docker compose \
    -f preprocessing/docker-compose.yaml \
    exec \
    -e OPENAI_API_KEY \
    -e ENHANCEMENT_MODEL \
    -e ENHANCEMENT_MAX_WORKERS \
    preprocessing \
    python -m preprocessing.enhancement.enhancement "$@"