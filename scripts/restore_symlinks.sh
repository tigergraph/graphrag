#!/usr/bin/env bash
# restore_symlinks.sh
# Restores symlinks for common/ and configs/ inside ecc/app/ and graphrag/app/
# Works in Git Bash on Windows (run as Administrator if Developer Mode is not enabled)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Enabling git core.symlinks for this repo..."
git config core.symlinks true

# Test symlink capability
TESTLINK="$REPO_ROOT/_symlink_test_"
if ! ln -s "$REPO_ROOT/common" "$TESTLINK" 2>/dev/null; then
    echo ""
    echo "[ERROR] Cannot create symlinks. Try one of:"
    echo "  1. Enable Developer Mode: Settings -> System -> For developers -> Developer Mode ON"
    echo "  2. Run Git Bash as Administrator"
    echo "  3. Run: powershell -ExecutionPolicy Bypass -File scripts\\restore_symlinks.ps1 -EnableDevMode"
    echo ""
    exit 1
fi
rm -f "$TESTLINK"

# Pairs: "link_path target_relative_to_link_parent"
declare -A SYMLINKS=(
    ["ecc/app/common"]="../../common"
    ["ecc/app/configs"]="../../configs"
    ["graphrag/app/common"]="../../common"
    ["graphrag/app/configs"]="../../configs"
)

for LINK in "${!SYMLINKS[@]}"; do
    TARGET="${SYMLINKS[$LINK]}"
    LINK_ABS="$REPO_ROOT/$LINK"
    LINK_DIR="$(dirname "$LINK_ABS")"
    TARGET_ABS="$(cd "$LINK_DIR" && realpath "$TARGET" 2>/dev/null || echo "$LINK_DIR/$TARGET")"

    if [ -e "$LINK_ABS" ] || [ -L "$LINK_ABS" ]; then
        if [ -L "$LINK_ABS" ]; then
            echo "  Removing existing symlink: $LINK"
        else
            echo "  Removing fake text file: $LINK"
        fi
        rm -rf "$LINK_ABS"
    fi

    if [ ! -e "$TARGET_ABS" ]; then
        echo "[WARN] Target does not exist: $TARGET_ABS — skipping $LINK"
        continue
    fi

    ln -s "$TARGET" "$LINK_ABS"
    echo "[OK] $LINK  ->  $TARGET"
done

echo ""
echo "==> Syncing git index..."
git checkout HEAD -- ecc/app/common ecc/app/configs graphrag/app/common graphrag/app/configs

echo ""
echo "Done! Status:"
git status --short ecc/app/common ecc/app/configs graphrag/app/common graphrag/app/configs
