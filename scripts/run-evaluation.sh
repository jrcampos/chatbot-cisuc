#!/usr/bin/env bash
set -euo pipefail

USAGE="Usage: $0 <extract|generate|validate|evaluate>... [-- arguments for a single stage]"

STAGES=()
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    extract|generate|validate|evaluate)
      STAGES+=("$1")
      shift
      ;;
    --)
      shift
      EXTRA_ARGS=("$@")
      break
      ;;
    *)
      echo "Unknown stage: $1" >&2
      echo "$USAGE" >&2
      exit 1
      ;;
  esac
done

if [[ ${#STAGES[@]} -eq 0 ]]; then
  echo "$USAGE" >&2
  exit 1
fi

if [[ ${#EXTRA_ARGS[@]} -gt 0 && ${#STAGES[@]} -gt 1 ]]; then
  echo "Arguments after -- can only be passed when running a single stage" >&2
  exit 1
fi

# Preserve a value explicitly provided by the caller (e.g. a pinned Chroma image).
PROVIDED_CHROMA_ANALYSIS_URL="${CHROMA_ANALYSIS_URL:-}"

set -a
source config/chatbot-common.env
source config/evaluation.env
set +a

# If local, source secrets; otherwise they come from CI.
if [[ -f secrets/evaluation.env ]]; then
  set -a
  source secrets/evaluation.env
  set +a
fi

if [[ -n "$PROVIDED_CHROMA_ANALYSIS_URL" ]]; then
  export CHROMA_ANALYSIS_URL="$PROVIDED_CHROMA_ANALYSIS_URL"
fi

for STAGE in "${STAGES[@]}"; do
  echo "Running evaluation stage: $STAGE"

  case "$STAGE" in
    extract)
      python3 tests/qa_generation/extract_corpus.py "${EXTRA_ARGS[@]}"
      ;;
    generate)
      python3 tests/qa_generation/generate_ground_truth.py "${EXTRA_ARGS[@]}"
      ;;
    validate)
      python3 tests/qa_generation/validate_ground_truth.py "${EXTRA_ARGS[@]}"
      ;;
    evaluate)
      python3 tests/rag_ravluator.py "${EXTRA_ARGS[@]}"
      ;;
  esac
done
