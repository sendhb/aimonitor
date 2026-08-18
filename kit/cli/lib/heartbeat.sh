# 供 cli/autoloop-* source。约定调用方已定义 LOG_DIR 和 NAME（"coder"|"reviewer"）。
#
# 心跳文件：每轮循环开始时更新，不管这轮结果如何——监控系统靠它的 mtime
# 判断"循环进程是不是卡死了"，跟循环内部单次任务成功/失败是两回事。
#
# 事件日志：每轮一行 JSON，供监控统计失败率、判断任务卡在某状态多久。
heartbeat() {
  date +%s > "$LOG_DIR/autoloop-$NAME.heartbeat"
}

emit_event() {
  # emit_event <task_id_or_-> <outcome>
  local task="${1:--}" outcome="$2"
  python3 -c '
import json, sys, time
path, task, outcome = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path, "a", encoding="utf-8") as f:
    f.write(json.dumps({"ts": time.time(), "task": task, "outcome": outcome}) + "\n")
' "$LOG_DIR/autoloop-$NAME-events.jsonl" "$task" "$outcome"
}
