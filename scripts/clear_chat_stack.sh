#!/usr/bin/env bash
# Clear GraphRAG UI chat state: TigerGraph conversation + message vertices.
# Run from repo root:  bash scripts/clear_chat_stack.sh
set -euo pipefail

echo "== TigerGraph: delete conversation + message vertices"
docker exec graphrag python /code/scripts/clear_chat_memory.py --all --yes

echo "== Done."
