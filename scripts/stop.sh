#!/usr/bin/env bash
# stop.sh — 停止 aimonitor 服务
set -euo pipefail

# 解析真实脚本路径（兼容通过根目录 symlink 执行）
SCRIPT="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SCRIPT")/.." && pwd)"
cd "$ROOT"

PID_FILE="/tmp/aimonitor-monitor-server.pid"
LOG="$ROOT/runtime/logs/monitor-server.log"

# 1) 优先按 PID 文件停止
if [[ -f "$PID_FILE" ]]; then
  PID="$(cat "$PID_FILE")"
  if kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
    echo "✓ 已停止 aimonitor (PID $PID)"
  else
    echo "⚠ PID $PID 已不存在（服务可能已退出），清理 PID 文件"
  fi
  rm -f "$PID_FILE"
  exit 0
fi

# 2) 兜底：按进程名查找（精确匹配 python3 服务进程，避免匹配到 shell 自身）
PIDS="$(pgrep -f "python3 server/monitor_server.py" || true)"
if [[ -n "$PIDS" ]]; then
  for PID in $PIDS; do
    kill "$PID" 2>/dev/null || true
  done
  echo "✓ 已停止 aimonitor (PID $PIDS, 兜底 pgrep)"
else
  echo "ℹ aimonitor 未在运行"
fi
