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
MAX_TASK_EVENTS = 200  # TASK-066：task 事件流单批上限（事件为结构化短记录，容量整体仍受 MAX_PAYLOAD_BYTES 约束）
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


def build_payload(project_id, snapshot, ts=None, task_events=None, cursor=None):
    """构造 AIOS 遥测 payload dict（应用条目数与单条内容截断）。

    参数:
        project_id: 项目 id（agent.json projects[].id，必填非空字符串）
        snapshot:   agent_runtime.read_project_runtime() 的返回 dict（6 个字段）
        ts:         事件时间戳（epoch 秒，int/float）；缺省取 time.time()
        task_events: TASK-066 可选——本次要推送的 task 事件增量（list[dict]，
            按 seq 递增；含 seq 字段）。None 表示未启用事件流（向后兼容，
            payload 不含 events/cursor 键）；空列表表示已启用但本轮无新事件。
        cursor:     TASK-066 可选——本次推送覆盖到的最大 seq（int 或 None）。
            仅在 task_events 非 None 时写入 payload。
    返回:
        {"project_id", "ts", "files": {...}}；启用事件流时额外含
        {"events": [...], "cursor": ...}
    异常:
        PayloadError: project_id 缺失/非字符串/空白；snapshot 非 dict；ts 非数字；
            task_events 非 list；cursor 非 int/None
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
    payload = {"project_id": project_id.strip(), "ts": ts, "files": files}
    if task_events is not None:
        if not isinstance(task_events, list):
            raise PayloadError("task_events 必须是 list（task 事件增量）")
        if cursor is not None and (isinstance(cursor, bool) or not isinstance(cursor, int)):
            raise PayloadError("cursor 必须是 int 或 None")
        included = task_events[:MAX_TASK_EVENTS]
        # SMELL-001（TASK-067）：截断发生时把 cursor 钳制到实际放入事件的最大 seq，
        # 使「payload 自身持有的 cursor 不变量」成立，不依赖调用方先截批。
        # 未截断或 events 为空时 cursor 原样保留（空 events 仅作心跳确认，无事件可钳）。
        if len(task_events) > MAX_TASK_EVENTS and cursor is not None:
            max_seq = None
            for ev in included:
                seq = ev.get("seq") if isinstance(ev, dict) else None
                if isinstance(seq, int) and not isinstance(seq, bool):
                    if max_seq is None or seq > max_seq:
                        max_seq = seq
            if max_seq is not None and cursor > max_seq:
                cursor = max_seq
        payload["events"] = included
        payload["cursor"] = cursor
    return payload


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
