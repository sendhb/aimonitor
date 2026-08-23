# pick_python — python 解释器分派（TASK-002）
# 背景：Windows 上 `python3` 常是 Microsoft Store stub（执行即报
#   "Python was not found; run without arguments to install from the Microsoft Store"），
#   真实 Python 通常只有 `python.exe`。Linux/macOS 则反之（只有 python3，python 常缺失）。
# 策略：
#   1) 优先 python3，其次 python
#   2) 排除路径含 WindowsApps 的 stub（不可执行）
#   3) 用 `-c 'import sys'` 实际探测可用性（防其它伪解释器）
# 输出解释器名（python3 / python）；无可用解释器时返回非 0。
# 用法：
#   source "$(dirname "${BASH_SOURCE[0]}")/pick_python.sh"
#   PY="$(pick_python)" || { echo "✗ 需要 python3 或 python" >&2; exit 1; }
pick_python() {
  local c p
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      p="$(command -v "$c")"
      case "$p" in *WindowsApps*) continue ;; esac
      if "$c" -c 'import sys' >/dev/null 2>&1; then
        echo "$c"
        return 0
      fi
    fi
  done
  return 1
}
