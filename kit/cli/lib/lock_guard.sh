# lock_guard.sh — 跨平台进程锁（TASK-012）
#
# 替代 util-linux flock：Git Bash/MSYS 不带 flock，用纯 Python（lib/lock.py）
# 实现同语义非阻塞排他锁（Windows: msvcrt.locking；POSIX: fcntl.flock）。
#
# 用法（脚本内参数解析完成后调用，传本脚本的剩余参数）:
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/lock_guard.sh"
#   lock_guard "$LOCK_FILE" "$@"
#
# 语义:
#   拿不到锁 → 打印提示并返回 1（调用方 set -e 下即退出）
#   拿到锁   → exec 重入本脚本（AIOS_LOCK_HELD=1），持锁覆盖整个运行期，
#              与 autoloop-* 原 `exec 9>f; flock -n 9` 语义一致（TASK-008/032）。
#   进程退出/被杀时 OS 自动释放锁。

_LG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/pick_python.sh
source "$_LG_DIR/pick_python.sh"

lock_guard() {
  local lock_file="$1"; shift
  [ "${AIOS_LOCK_HELD:-0}" = "1" ] && return 0
  local py
  py="$(pick_python)" || {
    echo "✗ 需要 python3 或 python（进程锁依赖，lib/lock.py）" >&2
    return 1
  }
  # 重新 exec：AIOS_LOCK_HELD=1 防止递归；"$0" 是被调用脚本自身，持锁下继续执行
  AIOS_LOCK_HELD=1 exec "$py" "$_LG_DIR/lock.py" hold "$lock_file" -- bash "$0" "$@"
}
