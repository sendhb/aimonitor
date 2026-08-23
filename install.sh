#!/usr/bin/env bash
# ============================================================
# install.sh — 安装 AIOS Framework（Linux/macOS 版）
#
# 作者: hb <sendhb@21cn.com>
#
# 用法:
#   bash <(curl -fsSL https://<host>/install.sh) [--dir <install-dir>]
#
# 说明:
#   - 默认安装到 ~/.aibase/
#   - 检查 git 与 python3（只检查+提示，不自动 sudo 安装）
#   - 不污染系统全局状态
# ============================================================
set -euo pipefail

INSTALL_DIR="${HOME}/.aibase"
REPO_URL="${AIOS_REPO_URL:-https://github.com/your-org/aibase.git}"

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --repo) REPO_URL="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

echo "═══ AIOS Framework 安装 ═══"
echo "install dir: $INSTALL_DIR"

# ---------- 依赖检查（只检查，不自动安装）----------
missing=0
if ! command -v git >/dev/null 2>&1; then
  echo "✗ 缺少 git"
  case "$(uname -s)" in
    Linux*)  echo "  安装: sudo apt install git   (Debian/Ubuntu)";;
    Darwin*) echo "  安装: brew install git";;
  esac
  missing=1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ 缺少 python3"
  case "$(uname -s)" in
    Linux*)  echo "  安装: sudo apt install python3";;
    Darwin*) echo "  安装: brew install python3";;
  esac
  missing=1
fi
[ "$missing" -eq 1 ] && { echo "请先安装缺失依赖后重试。"; exit 1; }

# ---------- 下载 ----------
echo "→ 克隆 aibase 到 $INSTALL_DIR ..."
if [ -d "$INSTALL_DIR/.git" ]; then
  echo "  已存在，git pull 更新..."
  git -C "$INSTALL_DIR" pull
else
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

# ---------- 完成 ----------
echo "✓ 安装完成"
echo ""
echo "    使用:"
echo "      cd /path/to/your-project"
echo "      python3 $INSTALL_DIR/kit/cli/init --profile backend --install-deps"
echo "      python3 $INSTALL_DIR/kit/cli/task list"
