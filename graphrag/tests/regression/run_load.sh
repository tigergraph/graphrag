#!/usr/bin/env bash
# GraphRAG Regression — Load Exported Graph into TigerGraph
#
# Place ExportedGraph.zip in:
#   graphrag/tests/test_questions/<DATASET>/ExportedGraph/ExportedGraph.zip
# then run from the repo root:
#
#   ./graphrag/tests/regression/run_load.sh --dataset Apple_SEQ_10
#   ./graphrag/tests/regression/run_load.sh --dataset Apple_SEQ_10 --graphname my_apple
#
# After loading, run evaluation with:
#   ./graphrag/tests/regression/run_eval.sh --dataset Apple_SEQ_10 --graphname <name>
#
# --graphname    : import under a different name (default: original exported name)
# --tg-container : TigerGraph container name (default: tigergraph)
# Override graphrag container: GRAPHRAG_CONTAINER env var

set -euo pipefail

# Prevent Git Bash (MSYS) from converting Unix paths in docker exec args
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

CONTAINER="${GRAPHRAG_CONTAINER:-graphrag}"
TG_CONTAINER="${TG_CONTAINER:-tigergraph}"
GSQL_BIN="/home/tigergraph/tigergraph/app/4.2.1/cmd/gsql"
REG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${REG_DIR}/_container_sync.sh"

# ── Parse args ────────────────────────────────────────────────────────────────
DATASET=""
GRAPHNAME=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dataset)        DATASET="$2";      shift 2 ;;
        --graphname)      GRAPHNAME="$2";    shift 2 ;;
        --tg-container)   TG_CONTAINER="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 1 ;;
    esac
done

[ -z "${DATASET}" ] && { echo "ERROR: --dataset is required." >&2; exit 1; }

# Copy the regression code + datasets into the container on demand.
sync_regression_to_container "${CONTAINER}" "${REG_DIR}"

# The dataset folder is copied into the graphrag container at /code/tests/test_questions/
EXPORT_ZIP="/code/tests/test_questions/${DATASET}/ExportedGraph/ExportedGraph.zip"
TG_IMPORT_DIR="/tmp/graphrag_regression_${DATASET}"
GSQL_SCRIPT="/tmp/graphrag_import_${DATASET}.gsql"

# ── Step 1: Detect original graph name from zip ───────────────────────────────
echo ""
echo "Step 1/2: Detecting graph name from ExportedGraph.zip ..."
echo "─────────────────────────────────────────────────────────────────"

ORIG_GRAPHNAME=$(docker exec "${CONTAINER}" python -c "
import zipfile, sys
with zipfile.ZipFile('${EXPORT_ZIP}') as z:
    for name in z.namelist():
        if name.startswith('DBImportExport_') and name.endswith('.gsql'):
            print(name.replace('DBImportExport_', '').replace('.gsql', ''))
            sys.exit(0)
sys.exit(1)
")

[ -z "${ORIG_GRAPHNAME}" ] && {
    echo "ERROR: Cannot read graph name from ${EXPORT_ZIP}" >&2
    echo "  Put ExportedGraph.zip in: graphrag/tests/test_questions/${DATASET}/ExportedGraph/" >&2
    exit 1
}

FINAL_GRAPHNAME="${GRAPHNAME:-${ORIG_GRAPHNAME}}"
echo "  Original  : ${ORIG_GRAPHNAME}"
echo "  Import as : ${FINAL_GRAPHNAME}"

# ── Step 2: Copy zip into TigerGraph container and import ─────────────────────
echo ""
echo "Step 2/2: Importing '${FINAL_GRAPHNAME}' into TigerGraph ..."
echo "─────────────────────────────────────────────────────────────────"

docker exec "${CONTAINER}" cat "${EXPORT_ZIP}" \
    | docker exec -i "${TG_CONTAINER}" bash -c "mkdir -p ${TG_IMPORT_DIR} && cat > ${TG_IMPORT_DIR}/ExportedGraph.zip"

echo "  Zip copied to ${TG_CONTAINER}:${TG_IMPORT_DIR}/ExportedGraph.zip"

# If rename needed: unzip, sed-replace graph name references, re-zip
if [ "${FINAL_GRAPHNAME}" != "${ORIG_GRAPHNAME}" ]; then
    echo "  Renaming ${ORIG_GRAPHNAME} → ${FINAL_GRAPHNAME} inside zip ..."
    docker exec \
        -e ORIG="${ORIG_GRAPHNAME}" \
        -e FNEW="${FINAL_GRAPHNAME}" \
        -e DIR="${TG_IMPORT_DIR}" \
        "${TG_CONTAINER}" bash -c '
            cd "${DIR}"
            unzip -q ExportedGraph.zip -d export_src/
            find export_src/ -name "*.gsql" | while read f; do
                sed -i "s/${ORIG}/${FNEW}/g" "$f"
                newname="$(dirname "$f")/$(basename "$f" | sed "s/${ORIG}/${FNEW}/g")"
                [ "$f" != "$newname" ] && mv "$f" "$newname"
            done
            rm ExportedGraph.zip
            cd export_src && zip -rq ../ExportedGraph.zip . && cd ..
            rm -rf export_src/
        '
    echo "  Rename done."
fi

# Write GSQL import script to a file inside TigerGraph container, then run it
docker exec -i "${TG_CONTAINER}" bash -c "cat > ${GSQL_SCRIPT}" << GSQLEOF
IMPORT GRAPH ${FINAL_GRAPHNAME} FROM "${TG_IMPORT_DIR}"
GSQLEOF

echo "  Running: IMPORT GRAPH ${FINAL_GRAPHNAME} FROM \"${TG_IMPORT_DIR}\" ..."
docker exec "${TG_CONTAINER}" \
    "${GSQL_BIN}" -u tigergraph -p tigergraph -f "${GSQL_SCRIPT}"

echo ""
echo "  ✓ Graph '${FINAL_GRAPHNAME}' imported successfully."
echo ""
echo "  Next — run evaluation:"
echo "    ./graphrag/tests/regression/run_eval.sh --dataset ${DATASET} --graphname ${FINAL_GRAPHNAME}"
echo "─────────────────────────────────────────────────────────────────"
