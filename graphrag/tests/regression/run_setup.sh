#!/usr/bin/env bash
# GraphRAG Regression — Graph Setup
#
# Runs the setup INSIDE the graphrag container via docker exec, so it reuses
# the container's Python environment (deepeval, langchain, common.config, etc.)
# — nothing needs to be installed locally.
#
# Requires the regression code + test_questions to be mounted into the
# container (see docker-compose.yml graphrag volumes). After adding the
# mounts, recreate the container once with:  docker-compose up -d graphrag
#
# Usage (from repo root, on the host):
#   ./graphrag/tests/regression/run_setup.sh --dataset Toppan
#   ./graphrag/tests/regression/run_setup.sh --dataset Toppan --skip-rebuild
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

docker exec -e PYTHONUNBUFFERED=1 "${CONTAINER}" \
    python /code/tests/regression/setup_graph.py "$@"
