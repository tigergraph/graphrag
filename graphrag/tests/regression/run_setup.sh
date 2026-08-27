#!/usr/bin/env bash
# GraphRAG Regression — Graph Setup
#
# Runs the setup INSIDE the graphrag container via docker exec, so it reuses
# the container's Python environment (deepeval, langchain, common.config, etc.)
# — nothing needs to be installed locally.
#
# The regression code + test_questions are copied into the container on demand
# (no bind mount needed).
#
# Usage (from repo root, on the host):
#   ./graphrag/tests/regression/run_setup.sh --dataset MyDataset
#   ./graphrag/tests/regression/run_setup.sh --dataset MyDataset --skip-rebuild
#
# Override the container name with GRAPHRAG_CONTAINER if needed.
# All arguments are forwarded to setup_graph.py.

set -euo pipefail

# Git Bash / MSYS on Windows rewrites "/code/..." into a Windows path
# (e.g. C:/Program Files/Git/code/...) before passing it to docker.
# Disable that POSIX path conversion so the container path is preserved.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

CONTAINER="${GRAPHRAG_CONTAINER:-graphrag}"
REG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${REG_DIR}/_container_sync.sh"

sync_regression_to_container "${CONTAINER}" "${REG_DIR}"

docker exec -w /code -e PYTHONUNBUFFERED=1 "${CONTAINER}" \
    python /code/tests/regression/setup_graph.py "$@"
