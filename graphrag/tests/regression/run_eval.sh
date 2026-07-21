#!/usr/bin/env bash
# GraphRAG Regression — Evaluation
#
# Runs the evaluation INSIDE the graphrag container via docker exec, so it
# reuses the container's Python environment (deepeval, langchain,
# common.config, the agent code) — nothing is installed locally.
#
# Requires the regression code + test_questions to be mounted into the
# container (see docker-compose.yml graphrag volumes).
#
# Usage (from repo root, on the host):
#
#   Planned Agent (default):
#     ./graphrag/tests/regression/run_eval.sh --dataset Toppan --graphname toppan_html --agent planned
#
#   ReAct Agent:
#     ./graphrag/tests/regression/run_eval.sh --dataset Toppan --graphname toppan_html --agent reactive
#
#   Classic engine (auto retriever):
#     ./graphrag/tests/regression/run_eval.sh --dataset Toppan --graphname toppan_html --agent classic
#
#   Classic with specific retriever:
#     ./graphrag/tests/regression/run_eval.sh --dataset Toppan --graphname toppan_html --agent classic --search-type hybridsearch
#
#   Detailed per-question output:
#     ./graphrag/tests/regression/run_eval.sh --dataset Toppan --graphname toppan_html --agent planned --detailed
#
#   Limit to first N questions (quick smoke test):
#     ./graphrag/tests/regression/run_eval.sh --dataset Toppan --graphname toppan_html --agent planned --limit 5
#
# Override the container name with GRAPHRAG_CONTAINER if needed.
# Results are written to graphrag/tests/regression/results/ on the host
# (the directory is mounted into the container).
# All arguments are forwarded to evaluator.py.

set -euo pipefail

# Git Bash / MSYS on Windows rewrites "/code/..." into a Windows path
# (e.g. C:/Program Files/Git/code/...) before passing it to docker.
# Disable that POSIX path conversion so the container path is preserved.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

CONTAINER="${GRAPHRAG_CONTAINER:-graphrag}"

# FORCE_COLOR=1 keeps the colourised output even though docker exec output is
# piped (no TTY). Set NO_COLOR=1 in your shell to turn colours off.
docker exec \
    -e PYTHONUNBUFFERED=1 \
    -e PYTHONWARNINGS=ignore \
    -e FORCE_COLOR="${FORCE_COLOR:-1}" \
    -e NO_COLOR="${NO_COLOR:-}" \
    "${CONTAINER}" \
    python /code/tests/regression/evaluator.py "$@"
