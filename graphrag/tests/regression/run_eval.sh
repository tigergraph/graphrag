#!/usr/bin/env bash
# GraphRAG Regression — Answer Correctness + Hallucination Evaluation
#
# Runs evaluator.py INSIDE the graphrag container via docker exec, so it
# reuses the container's Python environment (deepeval, langchain,
# common.config, the agent code) — nothing is installed locally.
#
# Metrics run by this script:
#   - Answer Correctness (DeepEval GEval) — requires answers.csv
#   - Hallucination Check                 — always runs
#
# For the Recall@K retrieval metric (requires ground_truth_contexts.csv),
# use run_recall.sh instead:
#   ./graphrag/tests/regression/run_recall.sh --dataset Multihop30 --graphname <name>
#
# The regression code + test_questions are copied into the container on demand
# (no bind mount needed); results are copied back to results/ afterward.
#
# Usage (from repo root, on the host):
#
#   Planned Agent (default):
#     ./graphrag/tests/regression/run_eval.sh --dataset Toppan --graphname toppan_html --mode planned
#
#   ReAct Agent:
#     ./graphrag/tests/regression/run_eval.sh --dataset Toppan --graphname toppan_html --mode reactive
#
#   Classic engine (auto retriever):
#     ./graphrag/tests/regression/run_eval.sh --dataset Toppan --graphname toppan_html --mode classic/auto
#
#   Classic with specific retriever:
#     ./graphrag/tests/regression/run_eval.sh --dataset Toppan --graphname toppan_html --mode classic/hybridsearch
#
#   Detailed per-question output:
#     ./graphrag/tests/regression/run_eval.sh --dataset Toppan --graphname toppan_html --mode planned --detailed
#
#   Limit to first N questions (quick smoke test):
#     ./graphrag/tests/regression/run_eval.sh --dataset Toppan --graphname toppan_html --mode planned --limit 5
#
# Override the container name with GRAPHRAG_CONTAINER if needed.
# Results are written to graphrag/tests/regression/results/ on the host.
# All arguments are forwarded to evaluator.py.

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

# FORCE_COLOR=1 keeps the colourised output even though docker exec output is
# piped (no TTY). Set NO_COLOR=1 in your shell to turn colours off.
set +e
docker exec \
    -w /code \
    -e PYTHONUNBUFFERED=1 \
    -e PYTHONWARNINGS=ignore \
    -e FORCE_COLOR="${FORCE_COLOR:-1}" \
    -e NO_COLOR="${NO_COLOR:-}" \
    "${CONTAINER}" \
    python /code/tests/regression/evaluator.py "$@"
rc=$?
set -e

copy_results_from_container "${CONTAINER}" "${REG_DIR}"
exit $rc
