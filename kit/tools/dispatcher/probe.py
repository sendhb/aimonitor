"""probe.py — 只读探测各项目 runtime/tasks/ 状态（kit/tools/dispatcher/ 探测层）。

TASK-069：Phase 3 调度器骨架的状态收集组件（只读，绝无写操作）。

职责：
- 本地条目：读 `runtime/tasks/` 下的 TASK-*.md，统计六种状态
  （open/in-progress/in-review/blocked/done/cancelled）计数 + 最近事件。
- 远端 agent 传输条目（TASK-037 起）：经 aimonitor /api/status 聚合读状态
  （不再无脑跳过）；未配 fetcher（旧行为）或 aimonitor 不可达时仍标 skipped。
- 复用 `kit/tools/telemetry/agent_runtime.py` 的只读接口
  （read_project_runtime / read_task_events），不修改它。

容错约定（与 agent_runtime 一致）：
- 项目路径不存在 / 没有 runtime/tasks/ → 六种计数全 0（有数据但为空）；
- runtime/logs 缺失或 task-events.jsonl 缺失 → latest_event=None；
- 读取永不抛异常（aimonitor 不可达 → skipped 而非异常）。
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

# 复用 agent_runtime 的只读层（同目录层级：kit/tools/dispatcher/ → ../telemetry/）
AGENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "telemetry"
)
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)
import agent_runtime  # noqa: E402

from agent_adapter import aimonitor_base_url  # noqa: E402,F401  # TASK-083 URL 归一化
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


def fetch_aimonitor_counts(server_url, project_id, http_fn=None):
    """GET aimonitor /api/status 聚合 → 该项目六态计数（TASK-037：agent 条目 probe）。

    server_url 为空 / 不可达 / 项目未登记 → None（probe 永不抛，调用方标 skipped）。
    http_fn 可注入（单测替身）：url → parsed dict；抛任意异常视同不可达。
    """
    if not server_url or not project_id:
        return None
    url = aimonitor_base_url(server_url) + "/api/status"
    try:
        if http_fn is not None:
            data = http_fn(url)
        else:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read(8 << 20).decode("utf-8", errors="replace"))
    except Exception:
        return None  # 替身/网络/解析异常一概视同不可达（probe 永不抛）
    if not isinstance(data, dict):
        return None
    for p in data.get("projects") or []:
        if isinstance(p, dict) and p.get("id") == project_id:
            summary = p.get("summary") or {}
            return {st: int(summary.get(st) or 0) for st in STATUSES}
    return None


def snapshot_from_aimonitor(server_url, entry, http_fn=None):
    """aimonitor /api/status 项目数据 → read_project_runtime 形状快照（TASK-037）。

    policy._first_candidate 消费的快照形状是 tasks=[{name, content}]（原始
    TASK 文件文本），远端无本地文件 → 合成伪 frontmatter（metadata 取
    aimonitor 已解析字段）。已知局限（TASK-037 备注）：aimonitor 任务负载
    暂无 approval-ref / rework-count 字段 → 合成为空/0 —— P0 治理仍
    fail-closed（approval-ref 空 → 拦截）；rework-count 乐观为 0，双跑风险
    由分配指纹 + state.py 超时回收兜底，字段补齐登记为后续项。
    不可达/项目未登记 → None（调用方告警跳过，不崩溃）。
    """
    if not server_url or not entry:
        return None
    url = aimonitor_base_url(server_url) + "/api/status"
    try:
        if http_fn is not None:
            data = http_fn(url)
        else:
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read(8 << 20).decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    project = next((p for p in data.get("projects") or []
                    if isinstance(p, dict) and p.get("id") == entry.id), None)
    if project is None:
        return None
    tasks = []
    for t in project.get("tasks") or []:
        if not isinstance(t, dict) or not t.get("id"):
            continue
        meta = {
            "status": t.get("status") or "unknown",
            "priority": t.get("priority") or "",
            "risk": t.get("risk") or "",
            "updated": t.get("updated") or "",
            "approval-ref": "",
            "rework-count": "0",
        }
        lines = [f"---", "name: %s" % (t.get("name") or t["id"]), "metadata:"]
        lines += ["  %s: %s" % (k, v) for k, v in meta.items()]
        lines += ["---", ""]
        tasks.append({"name": "%s.md" % (t.get("slug") or t["id"]),
                      "content": "\n".join(lines)})
    return {"tasks": tasks}


def scan_project(entry, status_fetcher=None):
    """对单个注册条目做只读状态收集（不抛异常）。

    返回 dict：
    - 本地条目：
        {"entry": entry, "skipped": False, "reason": None, "source": "local",
         "counts": {status: int, ...}, "latest_event": dict|None}
    - agent 条目 + status_fetcher（TASK-037）：fetcher(entry) 返回计数 dict →
        {"entry": entry, "skipped": False, "reason": None, "source": "aimonitor",
         "counts": {...}, "latest_event": None}；fetcher 返回 None（不可达）→
        {"entry": entry, "skipped": True, "reason": "aimonitor-unreachable", ...}
    - agent 条目无 fetcher（旧行为，向后兼容）：
        {"entry": entry, "skipped": True, "reason": "agent-transport",
         "counts": None, "latest_event": None}
    """
    if is_agent(entry):
        if status_fetcher is None:
            return {
                "entry": entry,
                "skipped": True,
                "reason": "agent-transport",
                "source": None,
                "counts": None,
                "latest_event": None,
            }
        counts = status_fetcher(entry)
        if counts is None:
            return {
                "entry": entry,
                "skipped": True,
                "reason": "aimonitor-unreachable",
                "source": None,
                "counts": None,
                "latest_event": None,
            }
        return {
            "entry": entry,
            "skipped": False,
            "reason": None,
            "source": "aimonitor",
            "counts": counts,
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
        "source": "local",
        "counts": counts,
        "latest_event": latest_event,
    }


def scan_projects(entries, status_fetcher=None):
    """遍历注册表条目收集状态；agent 条目经 status_fetcher 读 aimonitor 聚合
    （TASK-037），无 fetcher 时保持旧行为（标 skipped，不打印）。

    打印由 CLI 层负责；本函数只做收集，保持 probe 层纯只读 + 无副作用。
    """
    return [scan_project(entry, status_fetcher) for entry in entries]
