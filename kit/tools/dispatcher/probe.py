"""probe.py — 只读探测各项目 runtime/tasks/ 状态（kit/tools/dispatcher/ 探测层）。

TASK-069：Phase 3 调度器骨架的状态收集组件（只读，绝无写操作）。

职责：
- 本地条目：读 `runtime/tasks/` 下的 TASK-*.md，统计六种状态
  （open/in-progress/in-review/blocked/done/cancelled）计数 + 最近事件。
- 远端 agent 传输条目：跳过（不尝试读不存在的路径），由调用方打印
  `skipped(agent-transport)` + stderr 告警（v1 注册表处理边界）。
- 复用 `kit/tools/agent/agent_runtime.py` 的只读接口
  （read_project_runtime / read_task_events），不修改它。

容错约定（与 agent_runtime 一致）：
- 项目路径不存在 / 没有 runtime/tasks/ → 六种计数全 0（有数据但为空）；
- runtime/logs 缺失或 task-events.jsonl 缺失 → latest_event=None；
- 读取永不抛异常。
"""
import os
import re
import sys

# 复用 agent_runtime 的只读层（同目录层级：kit/tools/dispatcher/ → ../agent/）
AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "agent"
)
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)
import agent_runtime  # noqa: E402

from registry import is_agent, is_local  # noqa: E402

STATUSES = ("open", "in-progress", "in-review", "blocked", "done", "cancelled")


def _parse_status(content):
    """从 TASK 文件 frontmatter 提取 metadata.status；缺失/损坏返回 None。"""
    if not content:
        return None
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    in_metadata = False
    for line in lines[1:]:
        s = line.strip()
        if s == "---":
            break
        if s in ("metadata", "metadata:"):
            in_metadata = True
            continue
        if not in_metadata:
            continue
        m = re.match(r"^status:\s*(.*)$", s)
        if m:
            return m.group(1).strip() or None
    return None


def count_statuses(tasks):
    """对任务列表（read_project_runtime 的 tasks 形状）统计六种状态计数。"""
    counts = {status: 0 for status in STATUSES}
    for task in tasks or []:
        status = _parse_status(task.get("content"))
        if status in counts:
            counts[status] += 1
    return counts


def scan_project(entry):
    """对单个注册条目做只读状态收集（不抛异常）。

    返回 dict：
    - 本地条目：
        {"entry": entry, "skipped": False, "reason": None,
         "counts": {status: int, ...}, "latest_event": dict|None}
    - 远端 agent 条目：
        {"entry": entry, "skipped": True, "reason": "agent-transport",
         "counts": None, "latest_event": None}
    """
    if is_agent(entry):
        return {
            "entry": entry,
            "skipped": True,
            "reason": "agent-transport",
            "counts": None,
            "latest_event": None,
        }

    snapshot = agent_runtime.read_project_runtime(entry.path)
    counts = count_statuses(snapshot.get("tasks"))
    events = agent_runtime.read_task_events(entry.path) or []
    latest_event = events[-1] if events else None
    return {
        "entry": entry,
        "skipped": False,
        "reason": None,
        "counts": counts,
        "latest_event": latest_event,
    }


def scan_projects(entries):
    """遍历注册表条目收集状态；agent 条目标记 skipped（不打印）。

    打印由 CLI 层负责；本函数只做收集，保持 probe 层纯只读 + 无副作用。
    """
    return [scan_project(entry) for entry in entries]
