#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

docker run --rm \
    --volume "$ROOT_DIR:/workspace" \
    --workdir /workspace/preprocessing \
    python:3.12-slim \
    sh -c '
        python -m pip install \
            --disable-pip-version-check \
            --no-cache-dir \
            pip-tools==7.6.0

        pip-compile \
            --resolver=backtracking \
            --upgrade \
            --output-file=requirements.txt \
            requirements.in
    '