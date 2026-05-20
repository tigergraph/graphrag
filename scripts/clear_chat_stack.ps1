# Clear GraphRAG UI chat state: TigerGraph conversation + message vertices.
# Run from repo root:  powershell -ExecutionPolicy Bypass -File scripts/clear_chat_stack.ps1

$ErrorActionPreference = "Stop"

Write-Host "== TigerGraph: delete conversation + message vertices (all graphs with memory schema)"
docker exec graphrag python /code/scripts/clear_chat_memory.py --all --yes
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "== Done."
