# 供 cli/autoloop-* source。约定调用方已定义 LOG_DIR 和 NAME（"coder"|"reviewer"）。
#
# 心跳文件：每轮循环开始时更新，不管这轮结果如何——监控系统靠它的 mtime
# 判断"循环进程是不是卡死了"，跟循环内部单次任务成功/失败是两回事。
#
# 事件日志：每轮一行 JSON，供监控统计失败率、判断任务卡在某状态多久。
#
# python 解释器分派（TASK-002）：Windows 商店 stub / 缺失时回退 python。
# shellcheck source=cli/lib/pick_python.sh
source "$(dirname "${BASH_SOURCE[0]}")/pick_python.sh"

heartbeat() {
  date +%s > "$LOG_DIR/autoloop-$NAME.heartbeat"
}

emit_event() {
  # emit_event <task_id_or_-> <outcome>
  local task="${1:--}" outcome="$2" py
  py="$(pick_python)" || {
    echo "⚠ emit_event: 缺少 python3/python，跳过事件记录" >&2
    return 0   # 降级而非中断循环（set -e 下 return 非 0 会杀掉 autoloop）
  }
  "$py" -c '
import json, sys, time
path, task, outcome = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps({"ts": time.time(), "task": task, "outcome": outcome}) + "\n")
' "$LOG_DIR/autoloop-$NAME-events.jsonl" "$task" "$outcome"
}
