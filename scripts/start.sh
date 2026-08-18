#!/usr/bin/env bash
# start.sh — 构建并启动 aimonitor 服务（封装 scripts/build.sh + server/monitor_server.py）
set -euo pipefail

# 解析真实脚本路径（兼容通过根目录 symlink 执行：readlink -f 解开 symlink 链）
SCRIPT="$(readlink -f "${BASH_SOURCE[0]}")"
ROOT="$(cd "$(dirname "$SCRIPT")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-3113}"
LOG="$ROOT/runtime/logs/monitor-server.log"
PID_FILE="/tmp/aimonitor-monitor-server.pid"
BUILD=1
DEV=""
QUIET=""

usage() {
  cat <<'EOF'
用法: bash scripts/start.sh [选项]

选项:
  --port PORT   监听端口（默认 3113，或用环境变量 PORT）
  --dev         服务 src/（开发模式）；默认服务 dist/（生产构建产物）
  --no-build    跳过前端构建（默认先构建）
  --quiet       关闭后台轮询日志
  -h, --help    显示帮助
EOF
}

# 解析参数
while [[ $# -gt 0 ]]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --dev) DEV="--dev"; shift ;;
    --no-build) BUILD=0; shift ;;
    --quiet) QUIET="--quiet"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "✗ 未知参数: $1"; usage; exit 1 ;;
  esac
done

# 防重复启动：PID 文件存在且进程存活 → 退出
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  echo "⚠ aimonitor 已在运行 (PID $(cat "$PID_FILE"), 端口 $PORT)"
  echo "  日志: $LOG"
  exit 1
fi
rm -f "$PID_FILE"

# 构建前端（可选）
if [[ "$BUILD" == "1" ]]; then
  echo "▶ 构建前端 src/ → dist/ ..."
  bash scripts/build.sh
fi

# 启动后端
mkdir -p "$(dirname "$LOG")"
nohup python3 server/monitor_server.py --port "$PORT" $DEV $QUIET > "$LOG" 2>&1 &
echo $! > "$PID_FILE"
PID="$(cat "$PID_FILE")"

# 健康检查：最多等 5 秒
echo "▶ 等待服务就绪 (PID $PID, 端口 $PORT) ..."
for i in $(seq 1 5); do
  if curl -sf "http://localhost:$PORT/api/status" > /dev/null 2>&1; then
    echo "✓ aimonitor 已启动: http://localhost:$PORT/"
    echo "  API: http://localhost:$PORT/api/status"
    echo "  日志: $LOG"
    echo "  停止: bash scripts/stop.sh"
    exit 0
  fi
  sleep 1
done

echo "✗ 启动失败或健康检查超时，日志见 $LOG" >&2
rm -f "$PID_FILE"
exit 1
