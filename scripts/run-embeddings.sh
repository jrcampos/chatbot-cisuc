#!/usr/bin/env bash
set -euo pipefail

set -a
source config/chatbot-common.env
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
  -e OLLAMA_URL \
  -e CHROMA_COLLECTION \
  preprocessing \
  python -m preprocessing.embeddings.populate "$@"