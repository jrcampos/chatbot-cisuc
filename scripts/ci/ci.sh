#!/usr/bin/env bash
set -euo pipefail

python -m ruff format --check .
python -m ruff check .

python -m pytest \
  tests/unit \
  tests/integration \
  -m "not slow and not external and not llm" \
  --strict-markers \
  --maxfail=1 \
  -q