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
  -e LLM_PROVIDER \
  -e OPENAI_API_KEY \
  -e MODEL_EMBEDDINGS \
  -e LLM_URL \
  -e CHROMA_COLLECTION \
  preprocessing \
  python -m preprocessing.embeddings.populate "$@"