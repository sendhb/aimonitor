#!/usr/bin/env bash
# build.sh — 确定性构建：src/ → dist/（零网络、无依赖）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DIST="$ROOT/dist"
rm -rf "$DIST"
mkdir -p "$DIST/css" "$DIST/js"

cp "$ROOT/src/index.html" "$DIST/index.html"
cp "$ROOT/src/css/style.css" "$DIST/css/style.css"
cp "$ROOT/src/js/main.js" "$DIST/js/main.js"
cp "$ROOT/src/js/trend.js" "$DIST/js/trend.js"

echo "✓ build: src → dist 完成"
