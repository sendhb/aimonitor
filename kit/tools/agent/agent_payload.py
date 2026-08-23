"""agent_payload.py — AIOS 遥测 payload 构造与序列化（kit/tools/agent/ payload 层）。

消费 TASK-023 `agent_runtime.read_project_runtime()` 的 runtime 快照，输出通用遥测格式：

    {
      "project_id": "proj-1",
      "ts": 1786892400.0,
      "files": {
        "tasks":              [{"name": "TASK-001.md", "content": "..."}, ...] | None,
        "focus":              "CURRENT_FOCUS.md 原文" | None,
        "heartbeats":         [{"file": "x.heartbeat", "mtime": 1786892280.0}, ...] | None,
        "events":             [{"name": "x-events.jsonl", "content": "..."}, ...] | None,
        "verification_count": 3 | None,
        "review_count":       1 | None,
      }
    }

容量策略（TASK-023 备注：截断与容量上限是 payload 构造层的职责，读取层只拿原文）：
- 条目数上限：tasks / events / heartbeats 分别截断到 MAX_TASKS / MAX_EVENTS / MAX_HEARTBEATS 条（丢弃尾部）。
- 单条 content / focus 上限：MAX_CONTENT_CHARS 字符，超长截尾并追加 TRUNCATION_SUFFIX 标记。
- 整体上限：序列化后 UTF-8 字节数 ≤ MAX_PAYLOAD_BYTES，超出抛 PayloadTooLargeError
  （TASK-025 HTTP 客户端 / TASK-027 主循环据此跳过推送或告警，避免触发 ingest 413）。

None / 空语义与读取层一致并原样保留：
- None（目录缺失，无数据）保持 None；
- 空列表 / 0（目录存在但为空）保持空列表 / 0。
build_payload 不修改入参 snapshot（内部对 dict 做浅拷贝后再截断）。
"""
import json
import time

MAX_TASKS = 50
MAX_EVENTS = 50
MAX_HEARTBEATS = 50
MAX_CONTENT_CHARS = 4096
MAX_PAYLOAD_BYTES = 256 * 1024  # 256 KiB
TRUNCATION_SUFFIX = "…[truncated]"


class PayloadError(Exception):
    """payload 构造/序列化失败；CLI/主循环捕获后跳过本轮推送。"""


class PayloadTooLargeError(PayloadError):
    """payload 序列化后超出 MAX_PAYLOAD_BYTES 整体容量上限。"""


def _truncate_text(s):
    """超长文本截尾并追加标记；None 原样返回。"""
    if s is None or len(s) <= MAX_CONTENT_CHARS:
        return s
    return s[:MAX_CONTENT_CHARS] + TRUNCATION_SUFFIX


def _limit_entries(entries, limit):
    """列表截断到 limit 条；非列表 / None 原样返回（防御，正常入参为 list|None）。"""
    if not isinstance(entries, list):
        return entries
    return entries[:limit]


def _truncate_content_entries(entries, limit):
    """对带 content 的条目（tasks/events）逐条截断 content，再按 limit 截断条数。

    返回新列表，不修改入参（条目为 dict 时浅拷贝）。
    """
    if not isinstance(entries, list):
        return entries
    out = []
    for entry in entries[:limit]:
        if isinstance(entry, dict) and isinstance(entry.get("content"), str):
            entry = dict(entry)
            entry["content"] = _truncate_text(entry["content"])
        out.append(entry)
    return out


def build_payload(project_id, snapshot, ts=None):
    """构造 AIOS 遥测 payload dict（应用条目数与单条内容截断）。

    参数:
        project_id: 项目 id（agent.json projects[].id，必填非空字符串）
        snapshot:   agent_runtime.read_project_runtime() 的返回 dict（6 个字段）
        ts:         事件时间戳（epoch 秒，int/float）；缺省取 time.time()
    返回:
        {"project_id", "ts", "files": {tasks/focus/heartbeats/events/verification_count/review_count}}
    异常:
        PayloadError: project_id 缺失/非字符串/空白；snapshot 非 dict；ts 非数字
    """
    if not isinstance(project_id, str) or not project_id.strip():
        raise PayloadError("project_id 必须是非空字符串（agent.json projects[].id）")
    if not isinstance(snapshot, dict):
        raise PayloadError("snapshot 必须是 dict（agent_runtime.read_project_runtime 的返回）")
    if ts is None:
        ts = time.time()
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        raise PayloadError("ts 必须是数字（epoch 秒）")
    ts = float(ts)

    files = {
        "tasks": _truncate_content_entries(snapshot.get("tasks"), MAX_TASKS),
        "focus": _truncate_text(snapshot.get("focus")),
        "heartbeats": _limit_entries(snapshot.get("heartbeats"), MAX_HEARTBEATS),
        "events": _truncate_content_entries(snapshot.get("events"), MAX_EVENTS),
        "verification_count": snapshot.get("verification_count"),
        "review_count": snapshot.get("review_count"),
    }
    return {"project_id": project_id.strip(), "ts": ts, "files": files}


def _dumps(payload):
    """紧凑确定性 JSON：键排序、不转义非 ASCII、紧凑分隔符。"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def validate_capacity(payload):
    """容量上限校验：序列化后 UTF-8 字节数 ≤ MAX_PAYLOAD_BYTES。

    超限抛 PayloadTooLargeError；正常返回字节数（int），供调用方记录。
    """
    size = len(_dumps(payload).encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise PayloadTooLargeError(
            f"payload 序列化后 {size} bytes 超过容量上限 {MAX_PAYLOAD_BYTES} bytes"
        )
    return size


def serialize_payload(payload):
    """序列化为紧凑 JSON 字符串，并做整体容量校验（内部走 validate_capacity）。

    返回 str；超出 MAX_PAYLOAD_BYTES 抛 PayloadTooLargeError。
    """
    validate_capacity(payload)
    return _dumps(payload)
