#!/usr/bin/env bash
# cross-build.sh — cross-compile reach-link for Linux arm64, x86_64, and MIPS
# Requires: cross (https://github.com/cross-rs/cross) or cargo with appropriate targets
set -euo pipefail

BINARY_NAME="reach-link"
TARGETS=("aarch64-unknown-linux-musl" "x86_64-unknown-linux-musl" "mipsel-unknown-linux-musl" "mips-unknown-linux-musl")
LABELS=("linux-arm64" "linux-x86_64" "linux-mipsel" "linux-mips")
OUT_DIR="$(dirname "$0")/artifacts"

mkdir -p "$OUT_DIR"

for i in "${!TARGETS[@]}"; do
    TARGET="${TARGETS[$i]}"
    LABEL="${LABELS[$i]}"
    echo "==> Building for ${TARGET} (${LABEL})..."

    cross build --release --target "${TARGET}"

    SRC="target/${TARGET}/release/${BINARY_NAME}"
    DEST="${OUT_DIR}/${BINARY_NAME}-${LABEL}"
    cp "$SRC" "$DEST"
    chmod +x "$DEST"

    SHA=$(sha256sum "$DEST" | awk '{print $1}')
    echo "${SHA}  ${BINARY_NAME}-${LABEL}" > "${OUT_DIR}/${BINARY_NAME}-${LABEL}.sha256"
    echo "    SHA-256: ${SHA}"
done

echo ""
echo "Artifacts written to ${OUT_DIR}/"
ls -lh "${OUT_DIR}/"
